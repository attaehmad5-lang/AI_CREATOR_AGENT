"""Append-only, hash-chained audit trail for provider routing and governance.

This local log is tamper-evident, not a replacement for remote WORM storage,
signed checkpoints, or deployment access controls. It never accepts arbitrary
request payloads or credential values.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import interprocess_file_lock

_ZERO_HASH = "0" * 64
_HASH_RE = re.compile(r"[a-f0-9]{64}\\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\\Z")
_STATUS_RE = re.compile(r"[A-Z][A-Z0-9_]{1,79}\\Z")
_EVENT_TYPES = frozenset({
    "ROUTING_DECISION",
    "ROUTED_EXECUTION",
    "CREDENTIAL_ROTATION",
    "BILLING_APPROVAL",
    "PROVIDER_CONFIGURATION",
    "PROVIDER_ACCESS_CHANGE",
    "PROVIDER_VERIFICATION",
})
_OUTCOMES = frozenset({
    "REQUESTED", "ALLOWED", "DENIED", "SUCCEEDED", "FAILED", "UNKNOWN",
})
_METADATA_KEYS = frozenset({
    "category", "capability", "model", "model_version", "strategy",
    "candidate_count", "selected_provider", "fallback_used",
    "approval_required", "approval_granted", "approval_operation",
    "charge_outcome", "credit_outcome", "estimated_cost", "cost_unit",
    "credential_backend", "credential_version_id", "credential_present",
    "authentication_method", "endpoint_host", "enabled", "role",
    "target_principal_hash", "scope_count", "policy_status",
    "execution_started", "execution_success", "error_type", "latency_ms",
})
_EVENT_FIELDS = {
    "sequence", "event_id", "timestamp", "event_type", "actor_principal_id",
    "tenant_id", "project_id", "provider_id", "request_id", "outcome",
    "status_code", "metadata", "previous_hash", "event_hash",
}
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_EVENTS = 200_000


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _event_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _safe_identifier(value: Any, *, optional: bool = True) -> bool:
    return value is None and optional or (
        isinstance(value, str) and _ID_RE.fullmatch(value) is not None
    )


def _safe_metadata(metadata: Any) -> dict[str, Any] | None:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict) or len(metadata) > 24:
        return None
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or key not in _METADATA_KEYS:
            return None
        if isinstance(value, bool) or value is None:
            safe[key] = value
        elif isinstance(value, int):
            if not -(2**63) <= value <= 2**63 - 1:
                return None
            safe[key] = value
        elif isinstance(value, float):
            if not (value == value and abs(value) != float("inf")):
                return None
            safe[key] = value
        elif isinstance(value, str):
            if len(value) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                return None
            lowered = value.casefold()
            if any(marker in lowered for marker in (
                "bearer ", "token=", "api_key=", "password=", "secret=",
                "credential=", "-----begin", "authorization:",
            )):
                return None
            safe[key] = value
        else:
            return None
    return safe


class ProviderAuditLogManager:
    """Append-only NDJSON audit trail with serialized writes and SHA-256 chain checks."""

    def __init__(
        self,
        log_path: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 1_000_000:
            raise ValueError("max_events must be within 1..1000000")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 512 * 1024 * 1024:
            raise ValueError("max_bytes must be within 1024..536870912")
        self.log_path = Path(log_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        current = self.clock()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("timezone-aware audit clock required")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _validate_event(event: Any, expected_sequence: int, expected_previous: str) -> bool:
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
            return False
        if (
            type(event.get("sequence")) is not int
            or event["sequence"] != expected_sequence
            or not isinstance(event.get("event_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", event["event_id"]) is None
            or not isinstance(event.get("timestamp"), str)
            or not isinstance(event.get("event_type"), str)
            or event["event_type"] not in _EVENT_TYPES
            or not _safe_identifier(event.get("actor_principal_id"))
            or not _safe_identifier(event.get("tenant_id"))
            or not _safe_identifier(event.get("project_id"))
            or not _safe_identifier(event.get("provider_id"))
            or not _safe_identifier(event.get("request_id"))
            or event.get("outcome") not in _OUTCOMES
            or (
                event.get("status_code") is not None
                and (
                    not isinstance(event["status_code"], str)
                    or _STATUS_RE.fullmatch(event["status_code"]) is None
                )
            )
            or _safe_metadata(event.get("metadata")) != event.get("metadata")
            or event.get("previous_hash") != expected_previous
            or not isinstance(event.get("event_hash"), str)
            or _HASH_RE.fullmatch(event["event_hash"]) is None
        ):
            return False
        try:
            timestamp = datetime.fromisoformat(event["timestamp"])
        except (TypeError, ValueError, OverflowError):
            return False
        if timestamp.tzinfo is None:
            return False
        payload = {key: value for key, value in event.items() if key != "event_hash"}
        return _event_hash(payload) == event["event_hash"]

    def _read_and_verify(self) -> tuple[list[dict[str, Any]], str]:
        if not self.log_path.exists():
            return [], _ZERO_HASH
        try:
            stat = self.log_path.stat()
            if stat.st_size > self.max_bytes:
                raise ValueError("AUDIT_LOG_CAPACITY_EXCEEDED")
            raw = self.log_path.read_bytes()
        except OSError as exc:
            raise ValueError("AUDIT_LOG_READ_FAILED") from exc
        if not raw:
            return [], _ZERO_HASH
        if not raw.endswith(b"\\n"):
            raise ValueError("AUDIT_LOG_TRUNCATED")
        events: list[dict[str, Any]] = []
        previous = _ZERO_HASH
        for index, line in enumerate(raw.splitlines(), start=1):
            if not line or len(line) > 64 * 1024:
                raise ValueError("AUDIT_LOG_RECORD_INVALID")
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError("AUDIT_LOG_RECORD_INVALID") from exc
            if not self._validate_event(event, index, previous):
                raise ValueError("AUDIT_LOG_CHAIN_TAMPERED")
            events.append(event)
            previous = event["event_hash"]
            if len(events) > self.max_events:
                raise ValueError("AUDIT_LOG_EVENT_CAPACITY_EXCEEDED")
        return events, previous

    def record_event(
        self,
        event_type: str,
        *,
        outcome: str,
        status_code: str,
        actor_principal_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        provider_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one sanitized event; failure never falls back to an unlogged write."""
        if event_type not in _EVENT_TYPES:
            return {"status": "INVALID_AUDIT_EVENT_TYPE", "saved": False}
        if outcome not in _OUTCOMES:
            return {"status": "INVALID_AUDIT_OUTCOME", "saved": False}
        if not isinstance(status_code, str) or _STATUS_RE.fullmatch(status_code) is None:
            return {"status": "INVALID_AUDIT_STATUS_CODE", "saved": False}
        for value, label in (
            (actor_principal_id, "ACTOR"), (tenant_id, "TENANT"),
            (project_id, "PROJECT"), (provider_id, "PROVIDER"),
            (request_id, "REQUEST"),
        ):
            if not _safe_identifier(value):
                return {"status": f"INVALID_AUDIT_{label}_ID", "saved": False}
        safe_details = _safe_metadata(metadata)
        if safe_details is None:
            return {"status": "UNSAFE_AUDIT_METADATA", "saved": False}
        try:
            now = self._now()
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_AUDIT_CLOCK", "saved": False}
        try:
            lock_path = self.log_path.with_name(self.log_path.name + ".lock")
            with self._lock:
                with interprocess_file_lock(lock_path):
                    events, previous = self._read_and_verify()
                    if len(events) >= self.max_events:
                        return {"status": "AUDIT_LOG_EVENT_CAPACITY_EXCEEDED", "saved": False}
                    record = {
                        "sequence": len(events) + 1,
                        "event_id": uuid.uuid4().hex,
                        "timestamp": now.isoformat(),
                        "event_type": event_type,
                        "actor_principal_id": actor_principal_id,
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "provider_id": provider_id,
                        "request_id": request_id,
                        "outcome": outcome,
                        "status_code": status_code,
                        "metadata": safe_details,
                        "previous_hash": previous,
                    }
                    record["event_hash"] = _event_hash(record)
                    encoded = _canonical_json(record) + b"\\n"
                    current_size = self.log_path.stat().st_size if self.log_path.exists() else 0
                    if current_size + len(encoded) > self.max_bytes or len(encoded) > 64 * 1024:
                        return {"status": "AUDIT_LOG_CAPACITY_EXCEEDED", "saved": False}
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                    if hasattr(os, "O_BINARY"):
                        flags |= os.O_BINARY
                    descriptor = os.open(str(self.log_path), flags, 0o600)
                    try:
                        view = memoryview(encoded)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("AUDIT_LOG_APPEND_FAILED")
                            view = view[written:]
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    try:
                        os.chmod(self.log_path, 0o600)
                    except OSError:
                        pass
                    return {
                        "status": "AUDIT_EVENT_RECORDED",
                        "saved": True,
                        "sequence": record["sequence"],
                        "event_id": record["event_id"],
                        "event_hash": record["event_hash"],
                    }
        except ValueError as exc:
            code = str(exc) if str(exc).startswith("AUDIT_") else "AUDIT_LOG_INVALID"
            return {"status": code, "saved": False}
        except OSError:
            return {"status": "AUDIT_LOG_WRITE_FAILED", "saved": False}

    def verify_integrity(self) -> dict[str, Any]:
        """Verify every sequence, record schema, and link from the genesis hash."""
        try:
            lock_path = self.log_path.with_name(self.log_path.name + ".lock")
            with self._lock:
                with interprocess_file_lock(lock_path):
                    events, head = self._read_and_verify()
            return {
                "status": "AUDIT_CHAIN_VALID",
                "valid": True,
                "event_count": len(events),
                "head_sequence": len(events),
                "head_hash": head,
            }
        except ValueError as exc:
            code = str(exc) if str(exc).startswith("AUDIT_") else "AUDIT_LOG_INVALID"
            return {"status": code, "valid": False}
        except OSError:
            return {"status": "AUDIT_LOG_READ_FAILED", "valid": False}

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Read verified events only; tenant filtering is applied after full-chain validation."""
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            return {"status": "INVALID_AUDIT_CURSOR", "events": []}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            return {"status": "INVALID_AUDIT_LIMIT", "events": []}
        if not _safe_identifier(tenant_id):
            return {"status": "INVALID_AUDIT_TENANT_ID", "events": []}
        try:
            lock_path = self.log_path.with_name(self.log_path.name + ".lock")
            with self._lock:
                with interprocess_file_lock(lock_path):
                    events, head = self._read_and_verify()
            filtered = [
                event for event in events
                if event["sequence"] > after_sequence
                and (tenant_id is None or event["tenant_id"] == tenant_id)
            ]
            selected = filtered[:limit]
            return {
                "status": "AUDIT_EVENTS_READ",
                "verified": True,
                "events": selected,
                "next_sequence": selected[-1]["sequence"] if selected else after_sequence,
                "has_more": len(filtered) > len(selected),
                "head_hash": head,
            }
        except ValueError as exc:
            code = str(exc) if str(exc).startswith("AUDIT_") else "AUDIT_LOG_INVALID"
            return {"status": code, "verified": False, "events": []}
        except OSError:
            return {"status": "AUDIT_LOG_READ_FAILED", "verified": False, "events": []}
