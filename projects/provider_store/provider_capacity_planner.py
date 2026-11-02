"""Deterministic provider capacity planning and synthetic load simulation.

The simulator never calls providers or spends credits. It is intended for
repeatable pre-production sizing and regression tests, not a substitute for
production load tests or measured provider service-level evidence.
"""
from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Any

from .safe_numbers import finite_number

_PROVIDER_ID_MAX = 128


class ProviderCapacityPlanner:
    """Estimate concurrency/queue needs and simulate bounded request profiles."""

    def __init__(self, *, max_requests: int = 10000) -> None:
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or not 1 <= max_requests <= 100000
        ):
            raise ValueError("max_requests is outside safe limits")
        self.max_requests = max_requests

    @staticmethod
    def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if minimum <= value <= maximum else None

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]

    @staticmethod
    def _valid_provider_id(provider_id: Any) -> bool:
        return (
            isinstance(provider_id, str)
            and bool(provider_id.strip())
            and len(provider_id) <= _PROVIDER_ID_MAX
        )

    def plan(
        self,
        *,
        provider_id: str,
        target_requests_per_second: float,
        p95_latency_ms: float,
        max_concurrent: int,
        max_queue_size: int,
        target_utilization: float = 0.70,
        burst_window_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Return conservative demand, throughput, and queue sizing estimates."""
        if not self._valid_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "plan": None}
        target_rps = finite_number(
            target_requests_per_second, minimum=0.0001, maximum=100000
        )
        latency = finite_number(p95_latency_ms, minimum=0.1, maximum=3600000)
        utilization = finite_number(target_utilization, minimum=0.1, maximum=0.95)
        burst_window = finite_number(
            burst_window_seconds, minimum=0.1, maximum=3600
        )
        concurrency = self._bounded_int(max_concurrent, minimum=1, maximum=10000)
        queue_size = self._bounded_int(max_queue_size, minimum=0, maximum=100000)
        if (
            target_rps is None or latency is None or utilization is None
            or burst_window is None or concurrency is None or queue_size is None
        ):
            return {"status": "INVALID_CAPACITY_INPUT", "plan": None}

        required_concurrency = math.ceil(
            target_rps * (latency / 1000.0) / utilization
        )
        estimated_capacity = concurrency * 1000.0 / latency * utilization
        required_queue = math.ceil(target_rps * burst_window)
        concurrency_sufficient = required_concurrency <= concurrency
        queue_sufficient = required_queue <= queue_size
        return {
            "status": "CAPACITY_PLAN_AVAILABLE",
            "plan": {
                "provider_id": provider_id,
                "target_requests_per_second": target_rps,
                "p95_latency_ms": latency,
                "target_utilization": utilization,
                "max_concurrent": concurrency,
                "required_concurrency": required_concurrency,
                "estimated_sustainable_requests_per_second": estimated_capacity,
                "concurrency_sufficient": concurrency_sufficient,
                "burst_window_seconds": burst_window,
                "required_queue_slots_for_burst": required_queue,
                "max_queue_size": queue_size,
                "queue_sufficient": queue_sufficient,
                "capacity_sufficient": concurrency_sufficient and queue_sufficient,
                "concurrency_shortfall": max(0, required_concurrency - concurrency),
                "queue_shortfall": max(0, required_queue - queue_size),
            },
        }

    def simulate_load(
        self,
        *,
        provider_id: str,
        arrivals: list[dict[str, Any]],
        max_concurrent: int,
        max_queue_size: int,
    ) -> dict[str, Any]:
        """Simulate ordered arrivals with FIFO queueing and finite worker slots.

        Each arrival is {"arrival_ms": number, "service_ms": number}. The
        simulation is deterministic, bounded, and provider-network-free.
        """
        if not self._valid_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "simulation": None}
        concurrency = self._bounded_int(max_concurrent, minimum=1, maximum=10000)
        queue_limit = self._bounded_int(max_queue_size, minimum=0, maximum=100000)
        if concurrency is None or queue_limit is None:
            return {"status": "INVALID_LOAD_LIMIT", "simulation": None}
        if not isinstance(arrivals, list) or len(arrivals) > self.max_requests:
            return {"status": "INVALID_LOAD_PROFILE", "simulation": None}

        normalized: list[tuple[float, float]] = []
        previous_arrival = -1.0
        for item in arrivals:
            if not isinstance(item, dict) or set(item) != {"arrival_ms", "service_ms"}:
                return {"status": "INVALID_LOAD_PROFILE", "simulation": None}
            arrival = finite_number(item["arrival_ms"], minimum=0, maximum=86400000)
            service = finite_number(item["service_ms"], minimum=0.1, maximum=3600000)
            if arrival is None or service is None or arrival < previous_arrival:
                return {"status": "INVALID_LOAD_PROFILE", "simulation": None}
            normalized.append((arrival, service))
            previous_arrival = arrival

        running: list[float] = []
        waiting: deque[tuple[float, float]] = deque()
        queue_waits: list[float] = []
        accepted = 0
        rejected = 0
        max_depth = 0
        service_total = 0.0

        for arrival, service in normalized:
            while running and running[0] <= arrival:
                finished_at = heapq.heappop(running)
                if waiting:
                    queued_at, queued_service = waiting.popleft()
                    queue_waits.append(max(0.0, finished_at - queued_at))
                    heapq.heappush(running, finished_at + queued_service)
            if len(running) < concurrency:
                heapq.heappush(running, arrival + service)
                accepted += 1
                service_total += service
            elif len(waiting) < queue_limit:
                waiting.append((arrival, service))
                max_depth = max(max_depth, len(waiting))
                accepted += 1
                service_total += service
            else:
                rejected += 1

        while running and waiting:
            finished_at = heapq.heappop(running)
            queued_at, queued_service = waiting.popleft()
            queue_waits.append(max(0.0, finished_at - queued_at))
            heapq.heappush(running, finished_at + queued_service)

        finish_at = max(running) if running else (
            normalized[0][0] if normalized else 0.0
        )
        start_at = normalized[0][0] if normalized else 0.0
        elapsed = max(0.0, finish_at - start_at)
        utilization = (
            min(1.0, service_total / (concurrency * elapsed))
            if elapsed > 0 else (1.0 if service_total > 0 else 0.0)
        )
        count = len(normalized)
        return {
            "status": "LOAD_SIMULATION_COMPLETED",
            "simulation": {
                "provider_id": provider_id,
                "offered_requests": count,
                "accepted_requests": accepted,
                "rejected_requests": rejected,
                "rejection_rate": rejected / count if count else 0.0,
                "max_queue_depth": max_depth,
                "p95_queue_wait_ms": self._percentile(queue_waits, 0.95),
                "estimated_worker_utilization": utilization,
                "simulated_drain_time_ms": elapsed,
                "queue_wait_sample_count": len(queue_waits),
                "provider_calls_made": 0,
                "paid_usage_incurred": False,
            },
        }
