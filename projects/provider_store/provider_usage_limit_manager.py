"""Cross-process user/project/tenant request limits with idempotent reservations."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderUsageLimitManager:
    PERIODS = ("hourly", "daily", "monthly")
    SCOPE_TYPES = ("user", "project", "tenant")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        limits: dict[str, dict[str, int]],
        max_records: int = 100_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(limits, dict) or set(limits) - set(self.SCOPE_TYPES):
            raise ValueError("limits must contain only user/project/tenant scopes")
        normalized: dict[str, dict[str, int]] = {}
        for scope in self.SCOPE_TYPES:
            values = limits.get(scope, {})
            if not isinstance(values, dict) or set(values) - set(self.PERIODS):
                raise ValueError("invalid usage limit scope")
            normalized[scope] = {}
            for period, limit in values.items():
                if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 10_000_000:
                    raise ValueError("usage limits must be bounded non-negative integers")
                normalized[scope][period] = limit
        if not isinstance(max_records, int) or isinstance(max_records, bool) or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records is outside supported limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.limits = normalized
        self.max_records = max_records
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.RLock()
        with self._transaction():
            pass

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 160 and value.strip() == value and not any(ord(c) < 32 for c in value)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("usage limit clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _window_start(now: datetime, period: str) -> str:
        if period == "hourly":
            return now.replace(minute=0, second=0, microsecond=0).isoformat()
        if period == "daily":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "requests": {}}

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("requests"), dict):
            raise ValueError("usage limit state schema is invalid")
        requests = state["requests"]
        if len(requests) > self.max_records:
            raise ValueError("usage limit state exceeds configured record limit")
        for request_id, record in requests.items():
            if not self._valid_id(request_id) or not isinstance(record, dict):
                raise ValueError("usage limit request record is invalid")
            if (
                record.get("status") not in {"RESERVED", "COMMITTED", "RELEASED"}
                or not isinstance(record.get("scopes"), dict)
                or set(record["scopes"]) != set(self.SCOPE_TYPES)
                or not all(self._valid_id(record["scopes"].get(scope)) for scope in self.SCOPE_TYPES)
                or not isinstance(record.get("created_at"), str)
            ):
                raise ValueError("usage limit request record fields are invalid")
            try:
                parsed = datetime.fromisoformat(record["created_at"])
            except ValueError:
                raise ValueError("usage limit request timestamp is invalid") from None
            if parsed.tzinfo is None:
                raise ValueError("usage limit request timestamp must be timezone-aware")
        return state

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("usage limit state cannot be read") from None
        return self._validate_state(state)

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    class _Transaction:
        def __init__(self, manager: "ProviderUsageLimitManager") -> None:
            self.manager = manager
        def __enter__(self):
            self.manager._thread_lock.acquire()
            self.lock = interprocess_file_lock(self.manager.lock_path)
            self.lock.__enter__()
            try:
                self.state = self.manager._load()
            except Exception:
                self.lock.__exit__(None, None, None)
                self.manager._thread_lock.release()
                raise
            return self
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.manager._save(self.state)
            finally:
                self.lock.__exit__(exc_type, exc, tb)
                self.manager._thread_lock.release()
            return False

    def _transaction(self):
        return self._Transaction(self)

    def _usage_count(self, state: dict[str, Any], scope_type: str, scope_id: str, period: str, now: datetime) -> int:
        window_start = self._window_start(now, period)
        count = 0
        for record in state["requests"].values():
            if record.get("status") not in {"RESERVED", "COMMITTED"}:
                continue
            if record["scopes"].get(scope_type) != scope_id:
                continue
            try:
                created = datetime.fromisoformat(record["created_at"]).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
            if self._window_start(created, period) == window_start:
                count += 1
        return count

    def reserve(self, *, user_id: str, project_id: str, tenant_id: str, request_id: str) -> dict[str, Any]:
        if not all(self._valid_id(value) for value in (user_id, project_id, tenant_id, request_id)):
            return {"status": "INVALID_USAGE_LIMIT_REQUEST", "reserved": False}
        try:
            now = self._now()
        except Exception:
            return {"status": "USAGE_LIMIT_CLOCK_FAILED", "reserved": False}
        scopes = {"user": user_id, "project": project_id, "tenant": tenant_id}
        with self._transaction() as tx:
            state = tx.state
            existing = state["requests"].get(request_id)
            if existing is not None:
                if existing.get("scopes") != scopes:
                    return {"status": "REQUEST_ID_SCOPE_CONFLICT", "reserved": False}
                if existing.get("status") == "RESERVED":
                    return {"status": "ALREADY_RESERVED", "reserved": True}
                if existing.get("status") == "COMMITTED":
                    return {"status": "ALREADY_COMMITTED", "reserved": False}
                return {"status": "REQUEST_ALREADY_RELEASED", "reserved": False}
            if len(state["requests"]) >= self.max_records:
                return {"status": "USAGE_LIMIT_RECORD_CAPACITY_REACHED", "reserved": False}
            for scope_type, scope_id in scopes.items():
                for period, limit in self.limits[scope_type].items():
                    used = self._usage_count(state, scope_type, scope_id, period, now)
                    if used >= limit:
                        return {
                            "status": "USAGE_LIMIT_EXCEEDED",
                            "reserved": False,
                            "scope_type": scope_type,
                            "period": period,
                            "limit": limit,
                        }
            state["requests"][request_id] = {
                "status": "RESERVED", "scopes": scopes, "created_at": now.isoformat(),
            }
        return {"status": "RESERVED", "reserved": True, "request_id": request_id}

    def commit(self, *, request_id: str) -> dict[str, Any]:
        if not self._valid_id(request_id):
            return {"status": "INVALID_REQUEST_ID", "committed": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "REQUEST_NOT_FOUND", "committed": False}
            if record["status"] == "COMMITTED":
                return {"status": "ALREADY_COMMITTED", "committed": True}
            if record["status"] == "RELEASED":
                return {"status": "REQUEST_ALREADY_RELEASED", "committed": False}
            record["status"] = "COMMITTED"
        return {"status": "COMMITTED", "committed": True}

    def release(self, *, request_id: str) -> dict[str, Any]:
        if not self._valid_id(request_id):
            return {"status": "INVALID_REQUEST_ID", "released": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "REQUEST_NOT_FOUND", "released": False}
            if record["status"] == "RELEASED":
                return {"status": "ALREADY_RELEASED", "released": True}
            if record["status"] == "COMMITTED":
                return {"status": "COMMITTED_USAGE_CANNOT_BE_RELEASED", "released": False}
            record["status"] = "RELEASED"
        return {"status": "RELEASED", "released": True}
