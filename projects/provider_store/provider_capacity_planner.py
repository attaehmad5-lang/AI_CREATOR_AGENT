"""Conservative capacity estimates from measured or explicitly supplied workload inputs.

This module estimates worker concurrency; it does not run load tests or claim production
capacity. Use measured latency distributions and validate estimates with sustained tests.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


class ProviderCapacityPlanner:
    @staticmethod
    def _number(value: Any, *, name: str, minimum: float, maximum: float) -> float:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise ValueError(f"{name} is outside the supported range")
        return float(value)

    def estimate(
        self,
        *,
        target_requests_per_second: float,
        p95_latency_ms: float,
        target_utilization: float = 0.70,
        burst_factor: float = 1.5,
        max_workers: int = 10000,
    ) -> dict[str, Any]:
        rps = self._number(
            target_requests_per_second, name="target_requests_per_second",
            minimum=0.001, maximum=1_000_000,
        )
        latency = self._number(p95_latency_ms, name="p95_latency_ms", minimum=0.1, maximum=3_600_000)
        utilization = self._number(target_utilization, name="target_utilization", minimum=0.05, maximum=0.95)
        burst = self._number(burst_factor, name="burst_factor", minimum=1.0, maximum=100.0)
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or not 1 <= max_workers <= 1_000_000:
            raise ValueError("max_workers is outside the supported range")
        service_seconds = latency / 1000.0
        expected_in_flight = rps * service_seconds
        burst_concurrency = expected_in_flight * burst / utilization
        recommended_workers = max(1, math.ceil(burst_concurrency))
        return {
            "status": "CAPACITY_ESTIMATE",
            "target_requests_per_second": rps,
            "p95_latency_ms": latency,
            "target_utilization": utilization,
            "burst_factor": burst,
            "expected_in_flight_requests": round(expected_in_flight, 4),
            "recommended_workers": recommended_workers,
            "within_configured_worker_limit": recommended_workers <= max_workers,
            "configured_max_workers": max_workers,
            "production_load_test_required": True,
        }

    def summarize_samples(
        self,
        samples: Iterable[dict[str, Any]],
        *,
        window_seconds: float,
    ) -> dict[str, Any]:
        window = self._number(window_seconds, name="window_seconds", minimum=0.001, maximum=31_536_000)
        if isinstance(samples, (str, bytes, dict)):
            raise ValueError("samples must be an iterable of sample records")
        try:
            records = list(samples)
        except TypeError:
            raise ValueError("samples must be an iterable of sample records") from None
        if not records or len(records) > 1_000_000:
            raise ValueError("sample count must be between 1 and 1000000")
        latencies: list[float] = []
        successes = 0
        timeouts = 0
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("success"), bool) or not isinstance(record.get("timed_out", False), bool):
                raise ValueError("each sample requires boolean success and optional timed_out")
            if record.get("timed_out", False) and record["success"]:
                raise ValueError("a timed-out sample cannot also be successful")
            if record["success"]:
                successes += 1
            if record.get("timed_out", False):
                timeouts += 1
            latency = record.get("latency_ms")
            if latency is not None:
                latencies.append(self._number(latency, name="latency_ms", minimum=0.1, maximum=3_600_000))
        if not latencies:
            raise ValueError("at least one valid latency sample is required")
        ordered = sorted(latencies)
        p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
        count = len(records)
        return {
            "status": "LOAD_SAMPLE_SUMMARY",
            "sample_count": count,
            "window_seconds": window,
            "observed_requests_per_second": round(count / window, 6),
            "success_rate": round(successes / count, 6),
            "timeout_rate": round(timeouts / count, 6),
            "p95_latency_ms": ordered[p95_index],
            "capacity_estimate": self.estimate(
                target_requests_per_second=count / window,
                p95_latency_ms=ordered[p95_index],
            ),
            "production_load_test_required": True,
        }
