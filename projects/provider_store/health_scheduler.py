"""Bounded, poll-driven provider health scheduling with injectable probes and clocks."""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable

from .health_manager import ProviderHealthManager

_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")


class ProviderHealthScheduler:
    """Schedule health probes without creating hidden background threads.

    The application owns the polling loop and lifecycle. Every run is bounded by
    max_checks, and each injected probe receives a timeout_seconds argument.
    Probe implementations must enforce that timeout at their network boundary.
    """

    def __init__(
        self,
        health_manager: ProviderHealthManager,
        *,
        interval_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
        max_providers: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(health_manager, ProviderHealthManager):
            raise ValueError("health_manager must be a ProviderHealthManager")
        self.interval_seconds = self._positive_number(interval_seconds, "interval_seconds")
        self.timeout_seconds = self._positive_number(timeout_seconds, "timeout_seconds")
        if isinstance(max_providers, bool) or not isinstance(max_providers, int) or not 1 <= max_providers <= 10000:
            raise ValueError("max_providers must be an integer between 1 and 10000")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.health_manager = health_manager
        self.max_providers = max_providers
        self.clock = clock
        self._lock = threading.RLock()
        self._probes: dict[str, Callable[..., Any]] = {}
        self._next_due: dict[str, float] = {}

    @staticmethod
    def _positive_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive finite number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be a positive finite number")
        return number

    def register(
        self,
        provider_id: str,
        probe: Callable[..., Any],
        *,
        interval_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "registered": False}
        interval = (
            self.interval_seconds
            if interval_seconds is None
            else self._positive_number(interval_seconds, "interval_seconds")
        )
        with self._lock:
            if provider_id not in self._probes and len(self._probes) >= self.max_providers:
                return {"status": "SCHEDULER_CAPACITY_REACHED", "registered": False}
            self._probes[provider_id] = probe
            self._next_due[provider_id] = float(self.clock())
            # Per-provider intervals are stored separately from global defaults.
            if not hasattr(self, "_intervals"):
                self._intervals: dict[str, float] = {}
            self._intervals[provider_id] = interval
        return {"status": "PROBE_SCHEDULED", "registered": True, "provider": provider_id}

    def unregister(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            if provider_id not in self._probes:
                return {"status": "PROBE_NOT_SCHEDULED", "removed": False}
            self._probes.pop(provider_id, None)
            self._next_due.pop(provider_id, None)
            self._intervals.pop(provider_id, None)
        return {"status": "PROBE_UNSCHEDULED", "removed": True}

    def run_due(self, *, max_checks: int = 20) -> dict[str, Any]:
        if isinstance(max_checks, bool) or not isinstance(max_checks, int) or not 1 <= max_checks <= 1000:
            return {"status": "INVALID_CHECK_LIMIT", "checks": []}
        now = float(self.clock())
        if not math.isfinite(now):
            return {"status": "INVALID_CLOCK", "checks": []}
        with self._lock:
            due = sorted(
                (
                    (self._next_due[provider_id], provider_id, probe)
                    for provider_id, probe in self._probes.items()
                    if self._next_due.get(provider_id, float("inf")) <= now
                ),
                key=lambda item: (item[0], item[1]),
            )[:max_checks]
            for _, provider_id, _ in due:
                self._next_due[provider_id] = now + self._intervals[provider_id]
        results = [
            self.health_manager.active_probe(
                provider_id, probe, timeout_seconds=self.timeout_seconds
            )
            for _, provider_id, probe in due
        ]
        return {
            "status": "HEALTH_CHECKS_COMPLETED" if results else "NO_CHECKS_DUE",
            "checks": results,
            "checked_count": len(results),
            "scheduled_count": len(self._probes),
            "next_due": self.snapshot()["next_due"],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "HEALTH_SCHEDULE_AVAILABLE" if self._probes else "NO_HEALTH_PROBES_SCHEDULED",
                "scheduled_count": len(self._probes),
                "next_due": dict(sorted(self._next_due.items())),
            }
