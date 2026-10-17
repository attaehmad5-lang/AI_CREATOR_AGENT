"""Deny-by-default provider administration actions with durable, secret-safe audit.

This is an authorization boundary helper. Production callers must inject a trusted
identity/RBAC authorizer and only expose handlers backed by real application services.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderAdminManager:
    OPERATIONS = frozenset({
        "provider.enable",
        "provider.disable",
        "provider.configure",
        "policy.simulate",
        "policy.activate",
        "catalog.restore",
        "diagnostics.read",
    })
    _SECRET_MARKERS = ("token", "secret", "password", "api_key", "authorization", "cookie")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        authorizer: Callable[..., Any] | None = None,
        handlers: dict[str, Callable[..., Any]] | None = None,
        max_events: int = 5000,
    ) -> None:
        if not isinstance(max_events, int) or isinstance(max_events, bool) or not 1 <= max_events <= 50000:
            raise ValueError("max_events must be between 1 and 50000")
        if handlers is not None and not isinstance(handlers, dict):
            raise ValueError("handlers must be a mapping")
        if handlers and any(key not in self.OPERATIONS or not callable(value) for key, value in handlers.items()):
            raise ValueError("handler map contains an unsupported operation")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.authorizer = authorizer
        self.handlers = dict(handlers or {})
        self.max_events = max_events
        self._thread_lock = threading.RLock()
        self._ensure_file()

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 160 and not any(ord(c) < 32 for c in value)

    @classmethod
    def _contains_secret_key(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(key, str) and any(marker in key.casefold() for marker in cls._SECRET_MARKERS):
                    return True
                if cls._contains_secret_key(nested):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(cls._contains_secret_key(item) for item in value)
        return False

    @staticmethod
    def _request_fingerprint(subject: str, operation: str, resource: str) -> str:
        raw = "\\0".join((subject, operation, resource)).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _empty_state(self) -> dict[str, Any]:
        return {"schema_version": 1, "requests": {}, "events": []}

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("admin audit state schema is invalid")
        requests = state.get("requests")
        events = state.get("events")
        if not isinstance(requests, dict) or not isinstance(events, list) or len(events) > self.max_events:
            raise ValueError("admin audit state shape is invalid")
        for request_id, record in requests.items():
            if not self._valid_id(request_id) or not isinstance(record, dict):
                raise ValueError("admin request ledger is invalid")
            if not all(isinstance(record.get(key), str) for key in ("fingerprint", "status")):
                raise ValueError("admin request record is invalid")
        for event in events:
            if (
                not isinstance(event, dict)
                or not all(isinstance(event.get(key), str) for key in ("request_id", "subject_id", "operation", "resource", "status", "timestamp"))
                or event.get("operation") not in self.OPERATIONS
            ):
                raise ValueError("admin audit event is invalid")
        return state

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = self._empty_state()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("admin audit state cannot be read") from None
        return self._validate_state(state)

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _ensure_file(self) -> None:
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            if not self.path.exists():
                self._save_unlocked(self._empty_state())
            else:
                self._load_unlocked()

    def list_operations(self) -> list[str]:
        return sorted(self.OPERATIONS)

    def execute(
        self,
        *,
        subject_id: str,
        operation: str,
        resource: str,
        request_id: str,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not all(self._valid_id(value) for value in (subject_id, resource, request_id)):
            return {"allowed": False, "status": "INVALID_ADMIN_REQUEST"}
        if operation not in self.OPERATIONS:
            return {"allowed": False, "status": "UNSUPPORTED_ADMIN_OPERATION"}
        if parameters is not None and not isinstance(parameters, dict):
            return {"allowed": False, "status": "INVALID_ADMIN_PARAMETERS"}
        if self._contains_secret_key(parameters or {}):
            return {"allowed": False, "status": "SECRET_FIELD_NOT_ALLOWED"}
        if not callable(self.authorizer):
            return {"allowed": False, "status": "ADMIN_AUTHORIZER_REQUIRED"}
        handler = self.handlers.get(operation)
        if not callable(handler):
            return {"allowed": False, "status": "ADMIN_HANDLER_UNAVAILABLE"}

        try:
            authorized = self.authorizer(
                subject_id=subject_id, operation=operation, resource=resource
            )
        except Exception:
            return {"allowed": False, "status": "ADMIN_AUTHORIZATION_FAILED"}
        if authorized is not True:
            return {"allowed": False, "status": "ADMIN_FORBIDDEN"}

        fingerprint = self._request_fingerprint(subject_id, operation, resource)
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            existing = state["requests"].get(request_id)
            if existing is not None:
                if existing.get("fingerprint") != fingerprint:
                    return {"allowed": False, "status": "ADMIN_REQUEST_ID_CONFLICT"}
                return {"allowed": existing.get("status") == "SUCCEEDED", "status": "ADMIN_REQUEST_REPLAY"}
            event = {
                "request_id": request_id,
                "subject_id": subject_id,
                "operation": operation,
                "resource": resource,
                "status": "PENDING",
                "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            }
            state["requests"][request_id] = {"fingerprint": fingerprint, "status": "PENDING"}
            state["events"].append(event)
            state["events"] = state["events"][-self.max_events:]
            self._save_unlocked(state)

        try:
            result = handler(
                subject_id=subject_id,
                resource=resource,
                request_id=request_id,
                parameters=dict(parameters or {}),
            )
            success = isinstance(result, dict) and result.get("success") is True
        except Exception:
            success = False

        final_status = "SUCCEEDED" if success else "FAILED"
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            record = state["requests"].get(request_id)
            if not isinstance(record, dict) or record.get("fingerprint") != fingerprint:
                return {"allowed": False, "status": "ADMIN_AUDIT_COMMIT_FAILED"}
            record["status"] = final_status
            for event in reversed(state["events"]):
                if event.get("request_id") == request_id:
                    event["status"] = final_status
                    break
            self._save_unlocked(state)
        return {"allowed": success, "status": final_status, "request_id": request_id}
