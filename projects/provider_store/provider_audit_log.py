"""Tamper-evident, privacy-minimized provider operation audit log."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ZERO_HASH = "0" * 64
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\\Z")
_OPERATION_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,95}\\Z")
_OUTCOME_RE = re.compile(r"[A-Z][A-Z0-9_.:-]{0,95}\\Z")
_TRACE_RE = re.compile(r"[0-9a-f]{32}\\Z")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ProviderAuditLog:
    """Append-only HMAC-SHA256 event chain shared safely by local processes.

    The signing key is supplied by the application (preferably a secrets vault or
    deployment secret) and is never persisted. Events deliberately accept only
    operation identifiers and bounded correlation IDs; prompts, payloads,
    credentials, provider responses, and arbitrary metadata are not accepted.
    """

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        signing_key: bytes | str,
        key_id: str = "primary",
        max_events: int = 100_000,
        clock: Any = None,
    ) -> None:
        if isinstance(signing_key, str):
            key = signing_key.encode("utf-8")
        elif isinstance(signing_key, bytes):
            key = signing_key
        else:
            raise ValueError("audit signing key must be bytes or text")
        if len(key) < 32:
            raise ValueError("audit signing key must contain at least 32 bytes")
        if (
            not isinstance(key_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", key_id) is None
        ):
            raise ValueError("invalid audit signing key ID")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 1_000_000:
            raise ValueError("max_events must be within safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.signing_key = key
        self.key_id = key_id
        self.max_events = max_events
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": "1.0", "key_id": self.key_id,
            "events": [], "head_hash": _ZERO_HASH,
        }
        if self.state_path.exists():
            with self._transaction():
                pass

    def _digest(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self.signing_key, _canonical_json(payload), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _valid_optional_id(value: Any) -> bool:
        return value is None or (
            isinstance(value, str) and _ID_RE.fullmatch(value) is not None
        )

    def _reload(self) -> None:
        if not self.state_path.exists():
            self._data = {
                "version": "1.0", "key_id": self.key_id,
                "events": [], "head_hash": _ZERO_HASH,
            }
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("provider audit log cannot be read") from exc
        if (
            not isinstance(loaded, dict)
            or set(loaded) != {"version", "key_id", "events", "head_hash"}
            or loaded.get("version") != "1.0"
            or loaded.get("key_id") != self.key_id
            or not isinstance(loaded.get("events"), list)
            or len(loaded["events"]) > self.max_events
            or not isinstance(loaded.get("head_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", loaded["head_hash"]) is None
        ):
            raise ValueError("provider audit log schema or signing key mismatch")
        previous = _ZERO_HASH
        for sequence, event in enumerate(loaded["events"], start=1):
            if not isinstance(event, dict):
                raise ValueError("provider audit log integrity verification failed")
            if set(event) != {
                "sequence", "event_id", "timestamp", "operation", "outcome",
                "actor_ref", "provider_id", "tenant_id", "project_id",
                "request_id", "trace_id", "previous_hash", "event_hash",
            }:
                raise ValueError("provider audit log integrity verification failed")
            payload = {key: value for key, value in event.items() if key != "event_hash"}
            if (
                event.get("sequence") != sequence
                or not isinstance(event.get("event_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", event["event_id"]) is None
                or not isinstance(event.get("timestamp"), str)
                or not isinstance(event.get("operation"), str)
                or _OPERATION_RE.fullmatch(event["operation"]) is None
                or not isinstance(event.get("outcome"), str)
                or _OUTCOME_RE.fullmatch(event["outcome"]) is None
                or (
                    event.get("actor_ref") is not None
                    and (
                        not isinstance(event["actor_ref"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", event["actor_ref"]) is None
                    )
                )
                or any(
                    not self._valid_optional_id(event.get(field))
                    for field in ("provider_id", "tenant_id", "project_id", "request_id")
                )
                or (
                    event.get("trace_id") is not None
                    and (
                        not isinstance(event["trace_id"], str)
                        or _TRACE_RE.fullmatch(event["trace_id"]) is None
                    )
                )
                or event.get("previous_hash") != previous
                or not isinstance(event.get("event_hash"), str)
                or not hmac.compare_digest(event["event_hash"], self._digest(payload))
            ):
                raise ValueError("provider audit log integrity verification failed")
            previous = event["event_hash"]
        if loaded["head_hash"] != previous:
            raise ValueError("provider audit log head verification failed")
        self._data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    self._data, stream, ensure_ascii=False, indent=2,
                    sort_keys=True, allow_nan=False,
                )
                stream.write("\\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary_path, self.state_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise

    def record(
        self,
        *,
        operation: str,
        outcome: str,
        principal_id: str | None = None,
        provider_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one redacted audit event; arbitrary details are intentionally unsupported."""
        if not isinstance(operation, str) or _OPERATION_RE.fullmatch(operation) is None:
            return {"status": "INVALID_AUDIT_OPERATION", "saved": False}
        if not isinstance(outcome, str) or _OUTCOME_RE.fullmatch(outcome) is None:
            return {"status": "INVALID_AUDIT_OUTCOME", "saved": False}
        if any(
            not self._valid_optional_id(value)
            for value in (principal_id, provider_id, tenant_id, project_id, request_id)
        ):
            return {"status": "INVALID_AUDIT_IDENTIFIER", "saved": False}
        if trace_id is not None and (
            not isinstance(trace_id, str) or _TRACE_RE.fullmatch(trace_id) is None
        ):
            return {"status": "INVALID_AUDIT_TRACE_ID", "saved": False}
        actor_ref = (
            hmac.new(
                self.signing_key, b"provider-audit-actor:" + principal_id.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if principal_id is not None else None
        )
        try:
            timestamp = self.clock().astimezone(timezone.utc).isoformat()
        except Exception:
            return {"status": "AUDIT_CLOCK_FAILED", "saved": False}
        with self._transaction():
            events = self._data["events"]
            if len(events) >= self.max_events:
                return {"status": "AUDIT_CAPACITY_REACHED", "saved": False}
            event = {
                "sequence": len(events) + 1,
                "event_id": secrets.token_hex(16),
                "timestamp": timestamp,
                "operation": operation,
                "outcome": outcome,
                "actor_ref": actor_ref,
                "provider_id": provider_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "previous_hash": self._data["head_hash"],
            }
            event["event_hash"] = self._digest(event)
            self._data["events"].append(event)
            self._data["head_hash"] = event["event_hash"]
            self._save()
            return {
                "status": "AUDIT_EVENT_RECORDED",
                "saved": True,
                "sequence": event["sequence"],
                "event_id": event["event_id"],
                "event_hash": event["event_hash"],
            }

    def verify_integrity(self) -> dict[str, Any]:
        """Re-read and verify every event under the interprocess lock."""
        try:
            with self._transaction():
                return {
                    "status": "AUDIT_LOG_VERIFIED",
                    "valid": True,
                    "event_count": len(self._data["events"]),
                    "head_hash": self._data["head_hash"],
                    "key_id": self.key_id,
                }
        except Exception as exc:
            return {
                "status": "AUDIT_LOG_INTEGRITY_FAILED",
                "valid": False,
                "error_type": type(exc).__name__,
            }

    def read_events(
        self, *, after_sequence: int = 0, limit: int = 100,
        provider_id: str | None = None, operation: str | None = None,
    ) -> dict[str, Any]:
        """Read a bounded event page without returning raw actor identifiers."""
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            return {"status": "INVALID_AUDIT_CURSOR", "events": []}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            return {"status": "INVALID_AUDIT_LIMIT", "events": []}
        if not self._valid_optional_id(provider_id):
            return {"status": "INVALID_AUDIT_PROVIDER_FILTER", "events": []}
        if operation is not None and (
            not isinstance(operation, str) or _OPERATION_RE.fullmatch(operation) is None
        ):
            return {"status": "INVALID_AUDIT_OPERATION_FILTER", "events": []}
        with self._transaction():
            selected = [
                dict(event) for event in self._data["events"]
                if event["sequence"] > after_sequence
                and (provider_id is None or event["provider_id"] == provider_id)
                and (operation is None or event["operation"] == operation)
            ]
            page = selected[:limit]
            return {
                "status": "AUDIT_EVENTS_AVAILABLE",
                "events": page,
                "next_sequence": page[-1]["sequence"] if page else after_sequence,
                "has_more": len(selected) > len(page),
            }
