"""Append-only, secret-minimizing provider security audit events."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import interprocess_file_lock


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_ACTION = re.compile(r"provider\.[a-z][a-z.]{0,79}")
_EVENT_TYPES = {"authorization", "routing", "connection", "credential", "paid_usage", "provider_admin"}
_DECISIONS = {"allowed", "denied", "success", "failure", "unknown"}
_SECRET_MARKERS = ("token=", "api_key=", "password=", "secret=", "bearer ", "authorization:")


class ProviderAuditLog:
    """Persist allowlisted audit fields; reject arbitrary payloads and secret-like strings."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_file_bytes: int = 10_000_000,
        max_event_bytes: int = 4096,
    ) -> None:
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError("audit path is required")
        if (
            not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool)
            or max_file_bytes < 1024 or max_file_bytes > 1_000_000_000
        ):
            raise ValueError("max_file_bytes is out of range")
        if (
            not isinstance(max_event_bytes, int) or isinstance(max_event_bytes, bool)
            or max_event_bytes < 128 or max_event_bytes > 65536
            or max_event_bytes > max_file_bytes
        ):
            raise ValueError("max_event_bytes is out of range")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_file_bytes = max_file_bytes
        self.max_event_bytes = max_event_bytes
        self._lock = threading.RLock()

    @staticmethod
    def _safe_optional(value: Any, pattern: re.Pattern[str]) -> bool:
        return value is None or (isinstance(value, str) and bool(pattern.fullmatch(value)))

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(event, dict):
            return {"status": "INVALID_AUDIT_EVENT", "saved": False}
        allowed_keys = {
            "event_type", "decision", "action", "status", "project_id",
            "provider_id", "request_id",
        }
        if set(event) - allowed_keys:
            return {"status": "UNSUPPORTED_AUDIT_FIELDS", "saved": False}
        event_type = event.get("event_type")
        decision = event.get("decision")
        action = event.get("action")
        status = event.get("status")
        project_id = event.get("project_id")
        provider_id = event.get("provider_id")
        request_id = event.get("request_id")
        if (
            not isinstance(event_type, str) or event_type not in _EVENT_TYPES
            or not isinstance(decision, str) or decision not in _DECISIONS
        ):
            return {"status": "INVALID_AUDIT_EVENT", "saved": False}
        if not isinstance(action, str) or not _ACTION.fullmatch(action):
            return {"status": "INVALID_AUDIT_ACTION", "saved": False}
        if not isinstance(status, str) or not _IDENTIFIER.fullmatch(status):
            return {"status": "INVALID_AUDIT_STATUS", "saved": False}
        if not self._safe_optional(project_id, _IDENTIFIER):
            return {"status": "INVALID_AUDIT_PROJECT", "saved": False}
        if not self._safe_optional(provider_id, re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")):
            return {"status": "INVALID_AUDIT_PROVIDER", "saved": False}
        if not self._safe_optional(request_id, _IDENTIFIER):
            return {"status": "INVALID_AUDIT_REQUEST", "saved": False}
        values = [event_type, decision, action, status, project_id or "", provider_id or "", request_id or ""]
        if any(any(marker in value.casefold() for marker in _SECRET_MARKERS) for value in values):
            return {"status": "SECRET_LIKE_AUDIT_VALUE_REJECTED", "saved": False}
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "decision": decision,
            "action": action,
            "status": status,
        }
        for key, value in (
            ("project_id", project_id), ("provider_id", provider_id), ("request_id", request_id)
        ):
            if value is not None:
                record[key] = value
        try:
            encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > self.max_event_bytes:
                return {"status": "AUDIT_EVENT_TOO_LARGE", "saved": False}
            with self._lock, interprocess_file_lock(self.lock_path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size + len(encoded) > self.max_file_bytes:
                    return {"status": "AUDIT_LOG_CAPACITY_REACHED", "saved": False}
                with self.path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError as exc:
            return {"status": "AUDIT_WRITE_FAILED", "saved": False, "error_type": type(exc).__name__}
        return {"status": "AUDIT_EVENT_SAVED", "saved": True}
