"""Durable at-most-once provider execution claims keyed by request ID.

This ledger prevents this application from invoking an adapter twice for the same
request ID. It does not promise remote exactly-once execution: a STARTED request
whose outcome is ambiguous remains blocked and must be reconciled explicitly.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL_STATES = {
    "REJECTED", "NOT_STARTED", "COMPLETED", "FAILED", "UNKNOWN",
    "FINALIZATION_PARTIAL",
}
_ACTIVE_STATES = {"RESERVED", "STARTED"}
_ALL_STATES = _TERMINAL_STATES | _ACTIVE_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_trace_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _TRACE_ID_RE.fullmatch(value) is not None
        and any(char != "0" for char in value)
    )


def _valid_amount(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


class ProviderExecutionLedger:
    """Interprocess-safe idempotency ledger for provider execution requests."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_entries: int = 10000,
    ) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries < 1
        ):
            raise ValueError("max_entries must be a positive integer")
        self.path = Path(path)
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "requests": {}}
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            with interprocess_file_lock(self.path.with_name(self.path.name + ".lock")):
                self._reload()
                yield

    def _reload(self) -> None:
        if not self.path.exists():
            self.data = {"version": "1.0", "requests": {}}
            return
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider execution ledger") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("requests"), dict)
            or len(loaded["requests"]) > self.max_entries
        ):
            raise ValueError("invalid provider execution ledger")
        for request_id, record in loaded["requests"].items():
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID_RE.fullmatch(request_id)
                or not isinstance(record, dict)
                or not isinstance(record.get("status"), str)
                or record.get("status") not in _ALL_STATES
                or not isinstance(record.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"])
                or not isinstance(record.get("project_id"), str)
                or not _ID_RE.fullmatch(record["project_id"])
                or not isinstance(record.get("category"), str)
                or not _ID_RE.fullmatch(record["category"])
                or not _valid_amount(record.get("estimated_cost"))
                or not _valid_trace_id(record.get("trace_id"))
                or not isinstance(record.get("created_at"), str)
                or not isinstance(record.get("updated_at"), str)
            ):
                raise ValueError("invalid provider execution record")
            for key in ("created_at", "updated_at"):
                try:
                    datetime.fromisoformat(record[key])
                except ValueError as exc:
                    raise ValueError("invalid provider execution timestamp") from exc
            provider_id = record.get("provider_id")
            if provider_id is not None and (
                not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id)
            ):
                raise ValueError("invalid provider execution provider ID")
            result_status = record.get("result_status")
            if result_status is not None and (
                not isinstance(result_status, str)
                or not re.fullmatch(r"[A-Z0-9_.:-]{1,80}", result_status)
            ):
                raise ValueError("invalid provider execution result status")
        self.data = loaded

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def fingerprint_request(
        *,
        project_id: str,
        category: str,
        estimated_cost: float,
        routing_options: dict[str, Any] | None = None,
    ) -> str:
        options = {} if routing_options is None else routing_options
        if not isinstance(options, dict):
            raise ValueError("routing_options must be an object")
        safe_options: dict[str, str | int | float | bool | None] = {}
        for key, value in options.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,40}", key):
                raise ValueError("invalid routing option name")
            if isinstance(value, (set, frozenset, list, tuple)):
                normalized = list(value)
                if any(item is not None and not isinstance(item, (str, int, float, bool)) for item in normalized):
                    raise ValueError("routing option collections must contain scalar values")
                if any(isinstance(item, float) and not math.isfinite(item) for item in normalized):
                    raise ValueError("routing option numbers must be finite")
                if isinstance(value, (set, frozenset)):
                    normalized.sort(key=lambda item: (type(item).__name__, repr(item)))
                safe_options[key] = normalized
                continue
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("routing option values must be scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("routing option number must be finite")
            safe_options[key] = value
        canonical = json.dumps(
            {
                "project_id": project_id,
                "category": category,
                "estimated_cost": float(estimated_cost),
                "routing_options": safe_options,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def reserve(
        self,
        *,
        request_id: str,
        project_id: str,
        category: str,
        estimated_cost: float,
        trace_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "reserved": False}
        if not isinstance(project_id, str) or not _ID_RE.fullmatch(project_id):
            return {"status": "INVALID_PROJECT_ID", "reserved": False}
        if not isinstance(category, str) or not _ID_RE.fullmatch(category):
            return {"status": "INVALID_CATEGORY", "reserved": False}
        if not _valid_amount(estimated_cost):
            return {"status": "INVALID_ESTIMATED_COST", "reserved": False}
        if not _valid_trace_id(trace_id):
            return {"status": "INVALID_TRACE_ID", "reserved": False}
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            return {"status": "INVALID_FINGERPRINT", "reserved": False}

        with self._transaction():
            requests = self.data["requests"]
            existing = requests.get(request_id)
            if isinstance(existing, dict):
                if existing.get("fingerprint") != fingerprint:
                    return {"status": "REQUEST_ID_CONFLICT", "reserved": False}
                if existing.get("status") in _ACTIVE_STATES:
                    return {
                        "status": "REQUEST_IN_PROGRESS" if existing["status"] == "RESERVED" else "REQUEST_OUTCOME_UNKNOWN",
                        "reserved": False,
                        "existing_status": existing["status"],
                        "provider_id": existing.get("provider_id"),
                        "trace_id": existing.get("trace_id"),
                    }
                return {
                    "status": "DUPLICATE_REQUEST",
                    "reserved": False,
                    "existing_status": existing["status"],
                    "result_status": existing.get("result_status"),
                    "provider_id": existing.get("provider_id"),
                    "trace_id": existing.get("trace_id"),
                    "executed": existing["status"] in {"COMPLETED", "FAILED", "UNKNOWN", "FINALIZATION_PARTIAL"},
                }
            if len(requests) >= self.max_entries:
                return {"status": "LEDGER_CAPACITY_REACHED", "reserved": False}
            now = _now()
            requests[request_id] = {
                "project_id": project_id,
                "category": category,
                "estimated_cost": float(estimated_cost),
                "trace_id": trace_id,
                "fingerprint": fingerprint,
                "status": "RESERVED",
                "created_at": now,
                "updated_at": now,
            }
            self._save()
            return {"status": "RESERVED", "reserved": True, "trace_id": trace_id}

    def mark_started(self, *, request_id: str, provider_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "saved": False}
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        with self._transaction():
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "REQUEST_NOT_RESERVED", "saved": False}
            if record["status"] != "RESERVED":
                return {"status": "REQUEST_NOT_RESERVED", "saved": False}
            record["status"] = "STARTED"
            record["provider_id"] = provider_id
            record["updated_at"] = _now()
            self._save()
            return {"status": "STARTED", "saved": True}

    def finish(
        self,
        *,
        request_id: str,
        status: str,
        result_status: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "saved": False}
        if not isinstance(status, str) or status not in _TERMINAL_STATES:
            return {"status": "INVALID_TERMINAL_STATUS", "saved": False}
        if not isinstance(result_status, str) or not re.fullmatch(r"[A-Z0-9_.:-]{1,80}", result_status):
            return {"status": "INVALID_RESULT_STATUS", "saved": False}
        if provider_id is not None and (
            not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id)
        ):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        with self._transaction():
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "REQUEST_NOT_RESERVED", "saved": False}
            if record["status"] in _TERMINAL_STATES:
                if record.get("status") == status and record.get("result_status") == result_status:
                    return {"status": "ALREADY_FINISHED", "saved": True, "duplicate": True}
                return {"status": "REQUEST_TERMINAL_CONFLICT", "saved": False}
            record["status"] = status
            record["result_status"] = result_status
            if provider_id is not None:
                record["provider_id"] = provider_id
            record["updated_at"] = _now()
            self._save()
            return {"status": "FINISHED", "saved": True, "duplicate": False}

    def get(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID"}
        with self._transaction():
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "NOT_FOUND", "request_id": request_id}
            return {"status": "FOUND", "request": dict(record)}
