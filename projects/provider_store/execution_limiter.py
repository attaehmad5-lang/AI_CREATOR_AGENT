"""Bounded provider execution admission with per-provider concurrency limits.

This component is an admission-control primitive, not a job queue or durable scheduler.
Callers must release every acquired slot in a finally block. Pending waiters are
in-memory only and are lost on process restart.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from typing import Any


class ProviderExecutionLimiter:
    def __init__(
        self,
        *,
        max_concurrent: int = 8,
        max_pending: int = 32,
        per_provider_limits: dict[str, int] | None = None,
    ) -> None:
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or not 1 <= max_concurrent <= 100_000:
            raise ValueError("max_concurrent must be an integer between 1 and 100000")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or not 0 <= max_pending <= 1_000_000:
            raise ValueError("max_pending must be an integer between 0 and 1000000")
        if per_provider_limits is not None and not isinstance(per_provider_limits, dict):
            raise ValueError("per_provider_limits must be a mapping")
        limits: dict[str, int] = {}
        for provider_id, limit in (per_provider_limits or {}).items():
            if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", provider_id):
                raise ValueError("provider limit key is invalid")
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_concurrent:
                raise ValueError("provider concurrency limits must be positive and no greater than max_concurrent")
            limits[provider_id] = limit
        self.max_concurrent = max_concurrent
        self.max_pending = max_pending
        self.per_provider_limits = limits
        self._condition = threading.Condition()
        self._active: dict[str, str] = {}
        self._pending: deque[tuple[str, str]] = deque()

    @staticmethod
    def _valid_identifier(value: Any, *, maximum: int = 200) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(rf"[A-Za-z0-9._:-]{{1,{maximum}}}", value))

    def _provider_active_count(self, provider_id: str) -> int:
        return sum(1 for active_provider in self._active.values() if active_provider == provider_id)

    def _can_acquire(self, provider_id: str) -> bool:
        provider_limit = self.per_provider_limits.get(provider_id, self.max_concurrent)
        return (
            len(self._active) < self.max_concurrent
            and self._provider_active_count(provider_id) < provider_limit
        )

    def _has_earlier_same_provider(self, provider_id: str, request_id: str) -> bool:
        for queued_provider, queued_id in self._pending:
            if queued_id == request_id:
                return False
            if queued_provider == provider_id:
                return True
        return False

    def acquire(
        self,
        provider_id: str,
        request_id: str,
        *,
        timeout_seconds: float = 0.0,
    ) -> dict[str, Any]:
        if not self._valid_identifier(provider_id, maximum=120):
            return {"acquired": False, "status": "INVALID_PROVIDER_ID"}
        if not self._valid_identifier(request_id):
            return {"acquired": False, "status": "INVALID_REQUEST_ID"}
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
            or timeout_seconds > 3600
        ):
            return {"acquired": False, "status": "INVALID_WAIT_TIMEOUT"}

        started = time.monotonic()
        deadline = started + float(timeout_seconds)
        with self._condition:
            if request_id in self._active or any(item[1] == request_id for item in self._pending):
                return {"acquired": False, "status": "DUPLICATE_REQUEST"}
            earlier_same_provider = any(item[0] == provider_id for item in self._pending)
            if self._can_acquire(provider_id) and not earlier_same_provider:
                self._active[request_id] = provider_id
                return {
                    "acquired": True, "status": "ACQUIRED",
                    "provider": provider_id, "request_id": request_id, "wait_ms": 0.0,
                }
            if timeout_seconds == 0:
                return {"acquired": False, "status": "OVERLOADED"}
            if len(self._pending) >= self.max_pending:
                return {"acquired": False, "status": "QUEUE_FULL"}

            self._pending.append((provider_id, request_id))
            while True:
                if self._can_acquire(provider_id) and not self._has_earlier_same_provider(provider_id, request_id):
                    self._pending.remove((provider_id, request_id))
                    self._active[request_id] = provider_id
                    self._condition.notify_all()
                    return {
                        "acquired": True, "status": "ACQUIRED",
                        "provider": provider_id, "request_id": request_id,
                        "wait_ms": round((time.monotonic() - started) * 1000, 3),
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self._pending.remove((provider_id, request_id))
                    except ValueError:
                        pass
                    self._condition.notify_all()
                    return {"acquired": False, "status": "QUEUE_TIMEOUT"}
                self._condition.wait(remaining)

    def release(self, provider_id: str, request_id: str) -> dict[str, Any]:
        if not self._valid_identifier(provider_id, maximum=120) or not self._valid_identifier(request_id):
            return {"released": False, "status": "INVALID_IDENTIFIER"}
        with self._condition:
            active_provider = self._active.get(request_id)
            if active_provider is None:
                return {"released": False, "status": "NOT_ACTIVE"}
            if active_provider != provider_id:
                return {"released": False, "status": "PROVIDER_MISMATCH"}
            del self._active[request_id]
            self._condition.notify_all()
            return {"released": True, "status": "RELEASED"}

    def cancel_pending(self, request_id: str) -> dict[str, Any]:
        if not self._valid_identifier(request_id):
            return {"cancelled": False, "status": "INVALID_REQUEST_ID"}
        with self._condition:
            for entry in tuple(self._pending):
                if entry[1] == request_id:
                    self._pending.remove(entry)
                    self._condition.notify_all()
                    return {"cancelled": True, "status": "CANCELLED"}
            return {"cancelled": False, "status": "NOT_PENDING"}

    def summary(self) -> dict[str, Any]:
        with self._condition:
            active_by_provider: dict[str, int] = {}
            for provider_id in self._active.values():
                active_by_provider[provider_id] = active_by_provider.get(provider_id, 0) + 1
            return {
                "max_concurrent": self.max_concurrent,
                "max_pending": self.max_pending,
                "active": len(self._active),
                "pending": len(self._pending),
                "active_by_provider": active_by_provider,
            }
