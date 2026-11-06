"""Tamper-evident append-only provider routing, approval and credential audit log.

Audit events are intentionally allowlisted: prompts, credentials, secret references,
request bodies and arbitrary caller-supplied metadata are never accepted. Event identity
references are HMAC-pseudonymized with a deployment-managed key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import interprocess_file_lock

_ZERO_HASH = "0" * 64
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{1,63}\Z")
_PROVIDER_RE = re.compile(r"(?:\*|[A-Za-z0-9][A-Za-z0-9_-]{0,99})\Z")
_EVENT_TYPES = frozenset({
    "provider.authorization.decision",
    "provider.routing.decision",
    "provider.approval.decision",
    "provider.credential.rotation",
    "provider.configuration.change",
    "provider.connection.verification",
    "provider.execution.result",
})
_BILLING_MODES = frozenset({"ZERO_COST", "FREE_CREDITS", "PAID_APPROVED"})


class ProviderAuditLog:
    """Append-only HMAC hash-chained audit log with interprocess serialization.

    The signing key must come from a deployment secret store/environment and must
    not be saved beside the log. Keep backups and access controls outside this file:
    a hash chain detects edits/reordering but does not by itself prevent deletion of
    the entire log or restore of an old valid backup.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
        max_events: int = 100_000,
        max_file_bytes: int = 64 * 1024 * 1024,
        max_event_bytes: int = 16 * 1024,
    ) -> None:
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("audit signing key must contain at least 32 bytes")
        if clock is not None and not callable(clock):
            raise ValueError("audit clock must be callable")
        for value, name, minimum, maximum in (
            (max_events, "max_events", 1, 1_000_000),
            (max_file_bytes, "max_file_bytes", 1024, 1024 * 1024 * 1024),
            (max_event_bytes, "max_event_bytes", 512, 64 * 1024),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside safe bounds")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._key = bytes(signing_key)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_events = max_events
        self.max_file_bytes = max_file_bytes
        self.max_event_bytes = max_event_bytes
        self._lock = threading.RLock()

    def _pseudonym(self, field: str, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise ValueError("invalid audit identity reference")
        digest = hmac.new(
            self._key, (field + ":" + value).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return digest

    def _event(
        self,
        *,
        event_type: str,
        outcome: str,
        reason_code: str,
        actor_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        provider_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        operation: str | None = None,
        strategy: str | None = None,
        estimated_cost: float | None = None,
        billing_mode: str | None = None,
        approved: bool | None = None,
        credential_backend: str | None = None,
        credential_version_id: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in _EVENT_TYPES:
            raise ValueError("unsupported provider audit event type")
        if not isinstance(outcome, str) or not _CODE_RE.fullmatch(outcome):
            raise ValueError("invalid provider audit outcome")
        if not isinstance(reason_code, str) or not _CODE_RE.fullmatch(reason_code):
            raise ValueError("invalid provider audit reason code")
        if provider_id is not None and (
            not isinstance(provider_id, str) or not _PROVIDER_RE.fullmatch(provider_id)
        ):
            raise ValueError("invalid provider audit provider ID")
        if operation is not None and (
            not isinstance(operation, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", operation)
        ):
            raise ValueError("invalid provider audit operation")
        if strategy is not None and (
            not isinstance(strategy, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,47}", strategy)
        ):
            raise ValueError("invalid provider audit strategy")
        if billing_mode is not None and billing_mode not in _BILLING_MODES:
            raise ValueError("invalid provider audit billing mode")
        if approved is not None and not isinstance(approved, bool):
            raise ValueError("invalid provider audit approval flag")
        if estimated_cost is not None:
            if isinstance(estimated_cost, bool) or not isinstance(estimated_cost, (int, float)):
                raise ValueError("invalid provider audit estimated cost")
            try:
                estimated_cost = float(estimated_cost)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("invalid provider audit estimated cost") from None
            if not math.isfinite(estimated_cost) or estimated_cost < 0 or estimated_cost > 1e12:
                raise ValueError("invalid provider audit estimated cost")
        for value, label in (
            (credential_backend, "credential backend"),
            (credential_version_id, "credential version"),
        ):
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= 128
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
                or any(marker in value.casefold() for marker in (
                    "token=", "api_key=", "password=", "secret=", "bearer ",
                ))
            ):
                raise ValueError(f"invalid {label} audit metadata")
        timestamp = self.clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("audit clock must return an aware datetime")
        if trace_id is not None and (
            not isinstance(trace_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
            or trace_id == "0" * 32
        ):
            raise ValueError("invalid provider audit trace ID")
        event = {
            "event_id": secrets.token_hex(16),
            "occurred_at": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event_type": event_type,
            "actor_ref": self._pseudonym("actor", actor_id),
            "tenant_ref": self._pseudonym("tenant", tenant_id),
            "project_ref": self._pseudonym("project", project_id),
            "provider_id": provider_id,
            "request_ref": self._pseudonym("request", request_id),
            "trace_ref": self._pseudonym("trace", trace_id),
            "operation": operation,
            "outcome": outcome,
            "reason_code": reason_code,
            "strategy": strategy,
            "estimated_cost": estimated_cost,
            "billing_mode": billing_mode,
            "approved": approved,
            "credential_backend": credential_backend,
            "credential_version_ref": (
                self._pseudonym("credential-version", credential_version_id)
                if credential_version_id is not None else None
            ),
        }
        return event

    @staticmethod
    def _canonical(value: dict[str, Any]) -> bytes:
        try:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise ValueError("provider audit event is not canonical JSON") from None

    def _mac(self, sequence: int, event: dict[str, Any], previous_mac: str) -> str:
        material = {
            "version": 1,
            "sequence": sequence,
            "previous_mac": previous_mac,
            "event": event,
        }
        return hmac.new(self._key, self._canonical(material), hashlib.sha256).hexdigest()

    @staticmethod
    def _valid_loaded_event(event: Any) -> bool:
        required = {
            "event_id", "occurred_at", "event_type", "actor_ref", "tenant_ref",
            "project_ref", "provider_id", "request_ref", "trace_ref", "operation", "outcome",
            "reason_code", "strategy", "estimated_cost", "billing_mode",
            "approved", "credential_backend", "credential_version_ref",
        }
        if not isinstance(event, dict) or set(event) != required:
            return False
        if not isinstance(event.get("event_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", event["event_id"]):
            return False
        if event.get("event_type") not in _EVENT_TYPES:
            return False
        timestamp = event.get("occurred_at")
        try:
            parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
        except ValueError:
            parsed_time = None
        if parsed_time is None or parsed_time.tzinfo is None:
            return False
        for field in ("actor_ref", "tenant_ref", "project_ref", "request_ref", "trace_ref", "credential_version_ref"):
            value = event.get(field)
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                return False
        provider_id = event.get("provider_id")
        if provider_id is not None and (
            not isinstance(provider_id, str) or not _PROVIDER_RE.fullmatch(provider_id)
        ):
            return False
        operation = event.get("operation")
        if operation is not None and (
            not isinstance(operation, str)
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", operation)
        ):
            return False
        for field in ("outcome", "reason_code"):
            value = event.get(field)
            if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
                return False
        strategy = event.get("strategy")
        if strategy is not None and (
            not isinstance(strategy, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,47}", strategy)
        ):
            return False
        cost = event.get("estimated_cost")
        if cost is not None and (
            isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost)) or not 0 <= cost <= 1e12
        ):
            return False
        billing_mode = event.get("billing_mode")
        if billing_mode is not None and billing_mode not in _BILLING_MODES:
            return False
        approved = event.get("approved")
        if approved is not None and not isinstance(approved, bool):
            return False
        backend = event.get("credential_backend")
        if backend is not None and (
            not isinstance(backend, str) or not 1 <= len(backend) <= 128
            or any(ord(char) < 33 or ord(char) == 127 for char in backend)
        ):
            return False
        return True

    def _read_verified(self) -> tuple[list[dict[str, Any]], str]:
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise ValueError("provider audit path must not be a symbolic link")
        if not self.path.exists():
            return [], _ZERO_HASH
        if not self.path.is_file():
            raise ValueError("provider audit path is not a regular file")
        size = self.path.stat().st_size
        if size > self.max_file_bytes:
            raise ValueError("provider audit log exceeds the configured size limit")
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("provider audit log has an incomplete final record")
        records: list[dict[str, Any]] = []
        previous = _ZERO_HASH
        for line in raw.splitlines():
            if not line or len(line) > self.max_event_bytes:
                raise ValueError("provider audit record is empty or too large")
            try:
                envelope = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                raise ValueError("provider audit log contains malformed JSON") from None
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"version", "sequence", "previous_mac", "event", "mac"}
                or envelope.get("version") != 1
                or isinstance(envelope.get("sequence"), bool)
                or not isinstance(envelope.get("sequence"), int)
                or envelope["sequence"] != len(records) + 1
                or envelope.get("previous_mac") != previous
                or not self._valid_loaded_event(envelope.get("event"))
                or not isinstance(envelope.get("mac"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", envelope["mac"])
            ):
                raise ValueError("provider audit chain structure is invalid")
            expected = self._mac(envelope["sequence"], envelope["event"], previous)
            if not hmac.compare_digest(expected, envelope["mac"]):
                raise ValueError("provider audit chain integrity verification failed")
            records.append(envelope)
            previous = envelope["mac"]
            if len(records) > self.max_events:
                raise ValueError("provider audit log exceeds the configured event limit")
        return records, previous

    def record(self, **fields: Any) -> dict[str, Any]:
        """Append one allowlisted event and fsync it before reporting success."""
        try:
            event = self._event(**fields)
            with self._lock, interprocess_file_lock(self.lock_path):
                records, previous = self._read_verified()
                if len(records) >= self.max_events:
                    return {"status": "AUDIT_CAPACITY_REACHED", "saved": False}
                sequence = len(records) + 1
                envelope = {
                    "version": 1,
                    "sequence": sequence,
                    "previous_mac": previous,
                    "event": event,
                    "mac": self._mac(sequence, event, previous),
                }
                line = self._canonical(envelope) + b"\n"
                if len(line) > self.max_event_bytes:
                    return {"status": "AUDIT_EVENT_TOO_LARGE", "saved": False}
                if (self.path.stat().st_size if self.path.exists() else 0) + len(line) > self.max_file_bytes:
                    return {"status": "AUDIT_STORAGE_LIMIT_REACHED", "saved": False}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_symlink():
                    return {"status": "AUDIT_PATH_INVALID", "saved": False}
                descriptor = os.open(
                    self.path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    try:
                        os.fchmod(descriptor, 0o600)
                    except (AttributeError, OSError):
                        pass
                    written = 0
                    while written < len(line):
                        count = os.write(descriptor, line[written:])
                        if count <= 0:
                            raise OSError("provider audit append made no progress")
                        written += count
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                return {
                    "status": "AUDIT_EVENT_RECORDED",
                    "saved": True,
                    "event_id": event["event_id"],
                    "sequence": sequence,
                    "chain_mac": envelope["mac"],
                }
        except Exception as exc:
            return {
                "status": "AUDIT_WRITE_FAILED",
                "saved": False,
                "error_type": type(exc).__name__,
            }

    def verify(self) -> dict[str, Any]:
        """Verify the full append-only HMAC chain before any consumer trusts it."""
        try:
            with self._lock, interprocess_file_lock(self.lock_path):
                records, previous = self._read_verified()
            return {
                "status": "AUDIT_LOG_VERIFIED",
                "valid": True,
                "event_count": len(records),
                "chain_mac": previous,
            }
        except Exception as exc:
            return {
                "status": "AUDIT_LOG_INTEGRITY_FAILED",
                "valid": False,
                "error_type": type(exc).__name__,
            }

    def read_events(self, *, limit: int = 1000) -> dict[str, Any]:
        """Return metadata-only events after verifying the entire chain."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            return {"status": "INVALID_AUDIT_READ_LIMIT", "events": []}
        try:
            with self._lock, interprocess_file_lock(self.lock_path):
                records, _previous = self._read_verified()
            return {
                "status": "AUDIT_EVENTS_AVAILABLE" if records else "NO_AUDIT_EVENTS",
                "events": [dict(item["event"]) for item in records[-limit:]],
                "total_events": len(records),
            }
        except Exception as exc:
            return {
                "status": "AUDIT_LOG_INTEGRITY_FAILED",
                "events": [],
                "error_type": type(exc).__name__,
            }
