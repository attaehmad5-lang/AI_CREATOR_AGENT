"""Append-only, hash-chained provider routing, credential and approval audit log.

Records contain identifiers and bounded decision metadata only. Never pass
credentials, tokens, request payloads, headers, provider response bodies or
exception text to this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_OPERATION = re.compile(r"[a-z][a-z0-9_.:-]{0,79}\Z")
_OUTCOME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_TYPES = frozenset({
    "ROUTING_DECISION",
    "CREDENTIAL_ACCESS",
    "CREDENTIAL_ROTATION",
    "APPROVAL_DECISION",
})
_BILLING_MODES = frozenset({"ZERO_COST", "FREE_CREDITS", "PAID_APPROVED", "UNKNOWN"})
_FIELDS = frozenset({
    "sequence", "timestamp", "event_type", "actor_id", "tenant_id",
    "project_id", "request_id", "provider_id", "operation", "outcome",
    "billing_mode", "previous_hash", "event_hash",
})
_ZERO_HASH = "0" * 64
_MAX_EVENTS = 50_000
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_QUERY_LIMIT = 1000


class ProviderAuditLog:
    """Persist a bounded, append-only audit chain with integrity verification.

    Hash chaining detects edits, removals, insertions, and reordering when an
    earlier trusted hash is available. It is not a substitute for remote
    write-once storage or independently anchored checkpoints: a privileged
    actor able to rewrite the whole file can recompute the chain.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _valid_optional_id(value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            return False
        # IDs are metadata, never a place to smuggle credential-shaped values.
        sensitive_marker = re.search(
            r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"authorization|bearer|password|secret|private[_-]?key|credential)",
            value,
        )
        return sensitive_marker is None

    @staticmethod
    def _hash_event(event: dict[str, Any]) -> str:
        canonical = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "events": []}
        try:
            raw = self.path.read_bytes()
            if len(raw) > _MAX_FILE_BYTES:
                raise ValueError("audit log exceeds size limit")
            state = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("audit log is unreadable") from exc
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "events"}
            or type(state.get("version")) is not int
            or state["version"] != 1
            or not isinstance(state.get("events"), list)
            or len(state["events"]) > _MAX_EVENTS
        ):
            raise ValueError("audit log schema is invalid")
        return state

    def _verify_state(self, state: dict[str, Any]) -> tuple[bool, str, str | None]:
        previous_hash = _ZERO_HASH
        events = state.get("events")
        if not isinstance(events, list) or len(events) > _MAX_EVENTS:
            return False, previous_hash, "AUDIT_LOG_SCHEMA_INVALID"
        for index, record in enumerate(events, start=1):
            if not isinstance(record, dict) or set(record) != _FIELDS:
                return False, previous_hash, "AUDIT_RECORD_SCHEMA_INVALID"
            if type(record.get("sequence")) is not int or record["sequence"] != index:
                return False, previous_hash, "AUDIT_SEQUENCE_INVALID"
            timestamp = record.get("timestamp")
            if (
                isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or timestamp < 0 or timestamp > 4102444800
            ):
                return False, previous_hash, "AUDIT_TIMESTAMP_INVALID"
            if record.get("event_type") not in _EVENT_TYPES:
                return False, previous_hash, "AUDIT_EVENT_TYPE_INVALID"
            for field in ("actor_id", "tenant_id", "project_id", "request_id", "provider_id"):
                if not self._valid_optional_id(record.get(field)):
                    return False, previous_hash, "AUDIT_IDENTIFIER_INVALID"
            operation = record.get("operation")
            if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
                return False, previous_hash, "AUDIT_OPERATION_INVALID"
            outcome = record.get("outcome")
            if not isinstance(outcome, str) or _OUTCOME.fullmatch(outcome) is None:
                return False, previous_hash, "AUDIT_OUTCOME_INVALID"
            if record.get("billing_mode") not in _BILLING_MODES:
                return False, previous_hash, "AUDIT_BILLING_MODE_INVALID"
            if record.get("previous_hash") != previous_hash:
                return False, previous_hash, "AUDIT_CHAIN_LINK_INVALID"
            claimed_hash = record.get("event_hash")
            if not isinstance(claimed_hash, str) or _HASH.fullmatch(claimed_hash) is None:
                return False, previous_hash, "AUDIT_HASH_INVALID"
            unsigned = {key: value for key, value in record.items() if key != "event_hash"}
            if self._hash_event(unsigned) != claimed_hash:
                return False, previous_hash, "AUDIT_HASH_MISMATCH"
            previous_hash = claimed_hash
        return True, previous_hash, None

    def verify(self) -> dict[str, Any]:
        """Verify the entire chain and return only integrity metadata."""
        try:
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                state = self._load()
                valid, last_hash, error = self._verify_state(state)
        except (OSError, ValueError):
            return {"valid": False, "status": "AUDIT_LOG_UNREADABLE", "event_count": 0}
        return {
            "valid": valid,
            "status": "AUDIT_LOG_VALID" if valid else error,
            "event_count": len(state["events"]),
            "last_hash": last_hash,
        }

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise OSError("audit log size limit reached")
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
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
        self,
        *,
        event_type: str,
        outcome: str,
        operation: str,
        actor_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
        provider_id: str | None = None,
        billing_mode: str = "UNKNOWN",
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Append one strict metadata-only event after verifying the existing chain."""
        if event_type not in _EVENT_TYPES:
            return {"saved": False, "status": "INVALID_AUDIT_EVENT_TYPE"}
        if not isinstance(outcome, str) or _OUTCOME.fullmatch(outcome) is None:
            return {"saved": False, "status": "INVALID_AUDIT_OUTCOME"}
        if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
            return {"saved": False, "status": "INVALID_AUDIT_OPERATION"}
        if not all(self._valid_optional_id(value) for value in (
            actor_id, tenant_id, project_id, request_id, provider_id,
        )):
            return {"saved": False, "status": "INVALID_AUDIT_IDENTIFIER"}
        if billing_mode not in _BILLING_MODES:
            return {"saved": False, "status": "INVALID_AUDIT_BILLING_MODE"}
        current = time.time() if timestamp is None else timestamp
        if (
            isinstance(current, bool) or not isinstance(current, (int, float))
            or not math.isfinite(current) or not 0 <= current <= 4102444800
        ):
            return {"saved": False, "status": "INVALID_AUDIT_TIMESTAMP"}
        try:
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                state = self._load()
                valid, previous_hash, error = self._verify_state(state)
                if not valid:
                    return {"saved": False, "status": "AUDIT_LOG_INTEGRITY_FAILURE", "reason": error}
                if len(state["events"]) >= _MAX_EVENTS:
                    return {"saved": False, "status": "AUDIT_LOG_CAPACITY_REACHED"}
                event = {
                    "sequence": len(state["events"]) + 1,
                    "timestamp": float(current),
                    "event_type": event_type,
                    "actor_id": actor_id,
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "request_id": request_id,
                    "provider_id": provider_id,
                    "operation": operation,
                    "outcome": outcome,
                    "billing_mode": billing_mode,
                    "previous_hash": previous_hash,
                }
                record = {**event, "event_hash": self._hash_event(event)}
                state["events"].append(record)
                try:
                    self._save(state)
                except OSError:
                    return {"saved": False, "status": "AUDIT_LOG_WRITE_FAILED"}
        except (OSError, ValueError):
            return {"saved": False, "status": "AUDIT_LOG_INTEGRITY_FAILURE"}
        return {
            "saved": True,
            "status": "AUDIT_EVENT_RECORDED",
            "sequence": record["sequence"],
            "event_hash": record["event_hash"],
        }

    def list_events(
        self, *, limit: int = 100, tenant_id: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        """Read sanitized metadata after verifying the entire append-only chain."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_QUERY_LIMIT:
            return {"status": "INVALID_AUDIT_QUERY_LIMIT", "events": []}
        if tenant_id is not None and not self._valid_optional_id(tenant_id):
            return {"status": "INVALID_AUDIT_IDENTIFIER", "events": []}
        if event_type is not None and event_type not in _EVENT_TYPES:
            return {"status": "INVALID_AUDIT_EVENT_TYPE", "events": []}
        try:
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                state = self._load()
                valid, last_hash, error = self._verify_state(state)
                if not valid:
                    return {"status": "AUDIT_LOG_INTEGRITY_FAILURE", "events": []}
                events = [
                    dict(item) for item in state["events"]
                    if (tenant_id is None or item["tenant_id"] == tenant_id)
                    and (event_type is None or item["event_type"] == event_type)
                ][-limit:]
        except (OSError, ValueError):
            return {"status": "AUDIT_LOG_UNREADABLE", "events": []}
        return {
            "status": "AUDIT_EVENTS",
            "events": events,
            "event_count": len(events),
            "last_hash": last_hash,
        }
