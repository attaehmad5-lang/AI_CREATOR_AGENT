"""Tamper-evident, privacy-minimized provider audit event journal."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_EVENT_TYPES = {
    "authorization_decision", "routing_decision", "provider_configuration",
    "provider_enablement", "credential_rotation", "role_assignment",
    "provider_scope_update", "principal_disable", "retention_sweep",
}
_DETAIL_KEYS = {
    "operation", "status", "reason_code", "billing_mode", "model",
    "endpoint_host", "estimated_cost", "actual_cost", "latency_ms",
    "approval_id", "approval_status", "resource_count", "deleted",
    "failed", "error_type",
}
_GENESIS = "0" * 64
_MAX_EVENTS = 100_000


class ProviderAuditLog:
    """Append-only hash-chained journal authenticated with an external secret key.

    The key must be supplied by the deployment secret manager and must never be
    stored in the journal or repository. Events intentionally exclude request
    bodies, prompts, credentials, raw endpoints, and arbitrary caller metadata.
    """

    def __init__(
        self, path: str | os.PathLike[str], *, integrity_key: bytes,
        clock: Any = time.time,
    ) -> None:
        if not isinstance(integrity_key, bytes) or len(integrity_key) < 32:
            raise ValueError("audit integrity key must contain at least 32 bytes")
        if not callable(clock):
            raise ValueError("audit clock must be callable")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._key = bytes(integrity_key)
        self._clock = clock
        self._lock = threading.RLock()
        if not self.path.exists():
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                if not self.path.exists():
                    self._save({"version": 1, "events": []})
        self.verify()

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _safe_identifier(value: Any, *, optional: bool = True) -> str | None:
        if value is None and optional:
            return None
        if (
            not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("invalid audit identifier")
        return value.strip()

    @classmethod
    def _clean_details(cls, details: Any) -> dict[str, Any]:
        if details is None:
            return {}
        if not isinstance(details, dict) or len(details) > 20:
            raise ValueError("audit details must be a bounded mapping")
        clean: dict[str, Any] = {}
        for key, value in details.items():
            if not isinstance(key, str) or key not in _DETAIL_KEYS:
                raise ValueError("audit detail key is not allowlisted")
            if key in {"estimated_cost", "actual_cost", "latency_ms"}:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError("invalid numeric audit detail")
                clean[key] = float(value)
            elif key in {"deleted", "failed", "resource_count"}:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("invalid count audit detail")
                clean[key] = value
            elif value is None:
                continue
            elif not isinstance(value, str) or len(value) > 160 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise ValueError("invalid text audit detail")
            else:
                clean[key] = value
        return clean

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("audit journal is unavailable or malformed") from None
        if not isinstance(raw, dict) or set(raw) != {"version", "events"} or raw.get("version") != 1:
            raise ValueError("audit journal schema is invalid")
        events = raw.get("events")
        if not isinstance(events, list) or len(events) > _MAX_EVENTS:
            raise ValueError("audit journal event collection is invalid")
        previous = _GENESIS
        for index, event in enumerate(events, 1):
            if not isinstance(event, dict) or set(event) != {
                "sequence", "timestamp", "event_type", "actor_id", "tenant_id",
                "provider_id", "request_id", "trace_id", "outcome", "details",
                "previous_hash", "event_hash",
            }:
                raise ValueError("audit event schema is invalid")
            if event.get("sequence") != index or event.get("previous_hash") != previous:
                raise ValueError("audit chain sequence is invalid")
            timestamp = event.get("timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(float(timestamp)) or timestamp < 0:
                raise ValueError("audit event timestamp is invalid")
            if event.get("event_type") not in _EVENT_TYPES or event.get("outcome") not in {"allowed", "denied", "success", "failure", "attempted"}:
                raise ValueError("audit event classification is invalid")
            for key in ("actor_id", "tenant_id", "provider_id", "request_id", "trace_id"):
                value = event.get(key)
                if value is not None:
                    self._safe_identifier(value, optional=False)
            self._clean_details(event.get("details"))
            supplied = event.get("event_hash")
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            expected = hmac.new(self._key, self._canonical(unsigned), hashlib.sha256).hexdigest()
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
                raise ValueError("audit event integrity verification failed")
            previous = supplied
        return raw

    def _save(self, state: dict[str, Any]) -> None:
        payload = self._canonical(state)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            atomic_replace_with_retry(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def append(
        self, event_type: str, *, actor_id: str | None = None,
        tenant_id: str | None = None, provider_id: str | None = None,
        request_id: str | None = None, trace_id: str | None = None,
        outcome: str, details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if event_type not in _EVENT_TYPES:
            return {"saved": False, "status": "AUDIT_EVENT_TYPE_INVALID"}
        if outcome not in {"allowed", "denied", "success", "failure", "attempted"}:
            return {"saved": False, "status": "AUDIT_OUTCOME_INVALID"}
        try:
            clean = {
                "actor_id": self._safe_identifier(actor_id),
                "tenant_id": self._safe_identifier(tenant_id),
                "provider_id": self._safe_identifier(provider_id),
                "request_id": self._safe_identifier(request_id),
                "trace_id": self._safe_identifier(trace_id),
                "details": self._clean_details(details),
            }
            now = self._clock()
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)) or now < 0:
                raise ValueError("invalid audit clock")
        except (TypeError, ValueError):
            return {"saved": False, "status": "AUDIT_EVENT_INVALID"}
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"saved": False, "status": "AUDIT_INTEGRITY_FAILURE"}
            events = state["events"]
            if len(events) >= _MAX_EVENTS:
                return {"saved": False, "status": "AUDIT_CAPACITY_REACHED"}
            previous = events[-1]["event_hash"] if events else _GENESIS
            event = {
                "sequence": len(events) + 1, "timestamp": float(now),
                "event_type": event_type, **clean, "outcome": outcome,
                "previous_hash": previous,
            }
            event["event_hash"] = hmac.new(
                self._key, self._canonical(event), hashlib.sha256
            ).hexdigest()
            events.append(event)
            try:
                self._save(state)
            except OSError:
                return {"saved": False, "status": "AUDIT_PERSISTENCE_FAILED"}
            return {
                "saved": True, "status": "AUDIT_EVENT_RECORDED",
                "sequence": event["sequence"], "event_hash": event["event_hash"],
            }

    def verify(self) -> dict[str, Any]:
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"valid": False, "status": "AUDIT_INTEGRITY_FAILURE", "events": 0}
            return {"valid": True, "status": "AUDIT_CHAIN_VALID", "events": len(state["events"])}

    def read_events(self, *, limit: int = 100, after_sequence: int = 0) -> dict[str, Any]:
        if (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000
            or isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0
        ):
            return {"status": "INVALID_AUDIT_PAGE", "events": []}
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                events = self._load()["events"]
            except ValueError:
                return {"status": "AUDIT_INTEGRITY_FAILURE", "events": []}
            page = events[after_sequence:after_sequence + limit]
            return {
                "status": "AUDIT_EVENTS_READ", "events": [dict(item) for item in page],
                "next_sequence": after_sequence + len(page),
                "has_more": after_sequence + len(page) < len(events),
            }
