"""Bounded per-provider concurrency and backpressure for synchronous adapters.

Limits are process-local. Multi-process or multi-device deployments must use a shared
queue/lease service or transactional database for global concurrency guarantees.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable

from .safe_numbers import finite_number


class ProviderExecutionLimiter:
    """Limit active adapter calls and bound waiting work without leaking permits."""

    def __init__(
        self,
        limits: dict[str, int],
        *,
        max_waiting_per_provider: int = 0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(limits, dict) or not limits:
            raise ValueError("limits must be a non-empty provider-to-positive-integer mapping")
        for provider_id, limit in limits.items():
            if (
                not isinstance(provider_id, str) or not provider_id.strip()
                or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
            ):
                raise ValueError("each provider limit must use a non-empty ID and positive integer")
        if (
            not isinstance(max_waiting_per_provider, int)
            or isinstance(max_waiting_per_provider, bool)
            or max_waiting_per_provider < 0
            or max_waiting_per_provider > 100000
        ):
            raise ValueError("max_waiting_per_provider must be a non-negative integer")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.limits = dict(limits)
        self.max_waiting_per_provider = max_waiting_per_provider
        self.clock = clock or time.monotonic
        self._condition = threading.Condition(threading.RLock())
        self._active: dict[str, int] = {}
        self._waiting: dict[str, int] = {}

    @staticmethod
    def _cancelled(cancel_event: Any) -> bool:
        if cancel_event is None:
            return False
        is_set = getattr(cancel_event, "is_set", None)
        if not callable(is_set):
            return True
        try:
            return bool(is_set())
        except Exception:
            return True

    def run(
        self,
        provider_id: str,
        operation: Callable[[], Any],
        *,
        queue_timeout_seconds: float = 0.0,
        cancel_event: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER_ID", "acquired": False}
        if not callable(operation):
            return {"status": "INVALID_OPERATION", "acquired": False}
        limit = self.limits.get(provider_id)
        if limit is None:
            return {"status": "PROVIDER_LIMIT_NOT_CONFIGURED", "acquired": False}
        timeout = finite_number(queue_timeout_seconds, minimum=0)
        if timeout is None:
            return {"status": "INVALID_QUEUE_TIMEOUT", "acquired": False}
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            return {"status": "INVALID_CANCEL_EVENT", "acquired": False}
        deadline = self.clock() + timeout
        waiting_registered = False
        with self._condition:
            while self._active.get(provider_id, 0) >= limit:
                if self._cancelled(cancel_event):
                    if waiting_registered:
                        self._waiting[provider_id] -= 1
                    return {"status": "EXECUTION_CANCELLED", "acquired": False}
                if not waiting_registered:
                    if self._waiting.get(provider_id, 0) >= self.max_waiting_per_provider:
                        return {"status": "EXECUTION_CAPACITY_REJECTED", "acquired": False}
                    self._waiting[provider_id] = self._waiting.get(provider_id, 0) + 1
                    waiting_registered = True
                remaining = deadline - self.clock()
                if remaining <= 0:
                    self._waiting[provider_id] -= 1
                    return {"status": "EXECUTION_QUEUE_TIMEOUT", "acquired": False}
                self._condition.wait(timeout=min(remaining, 0.1))
            if waiting_registered:
                self._waiting[provider_id] -= 1
            if self._cancelled(cancel_event):
                return {"status": "EXECUTION_CANCELLED", "acquired": False}
            self._active[provider_id] = self._active.get(provider_id, 0) + 1
        try:
            return {"status": "EXECUTED", "acquired": True, "result": operation()}
        finally:
            with self._condition:
                self._active[provider_id] = max(0, self._active.get(provider_id, 1) - 1)
                self._condition.notify_all()

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._condition:
            return {
                provider_id: {
                    "limit": limit,
                    "active": self._active.get(provider_id, 0),
                    "waiting": self._waiting.get(provider_id, 0),
                }
                for provider_id, limit in self.limits.items()
            }
