"""Bounded provider success, failure, latency, queue-wait, and timeout metrics."""
from __future__ import annotations

import math
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .safe_numbers import finite_number

_PROVIDER_ID_MAX = 128
_CATEGORY_MAX = 64


class ProviderMetricsManager:
    """In-process bounded metrics aggregator; never stores prompts, payloads, or tokens."""

    def __init__(self, *, max_samples: int = 2000, clock: Any = None) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 100000:
            raise ValueError("max_samples must be within safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.max_samples = max_samples
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)

    def record(
        self,
        *,
        provider_id: str,
        category: str,
        success: bool,
        latency_ms: float,
        queue_wait_ms: float = 0.0,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > _PROVIDER_ID_MAX:
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not isinstance(category, str) or not category.strip() or len(category) > _CATEGORY_MAX:
            return {"status": "INVALID_CATEGORY", "saved": False}
        if not isinstance(success, bool) or not isinstance(timed_out, bool):
            return {"status": "INVALID_METRIC_FLAGS", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        queue_wait = finite_number(queue_wait_ms, minimum=0)
        if latency is None or queue_wait is None:
            return {"status": "INVALID_METRIC_TIMING", "saved": False}
        sample = {
            "provider_id": provider_id,
            "category": category,
            "success": success,
            "latency_ms": latency,
            "queue_wait_ms": queue_wait,
            "timed_out": timed_out,
            "recorded_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        with self._lock:
            self._samples.append(sample)
        return {"status": "METRIC_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def snapshot(self, *, provider_id: str | None = None, category: str | None = None) -> dict[str, Any]:
        if provider_id is not None and (
            not isinstance(provider_id, str) or not provider_id.strip()
        ):
            return {"status": "INVALID_PROVIDER_ID", "metrics": None}
        if category is not None and (
            not isinstance(category, str) or not category.strip()
        ):
            return {"status": "INVALID_CATEGORY", "metrics": None}
        with self._lock:
            samples = [
                dict(item) for item in self._samples
                if (provider_id is None or item["provider_id"] == provider_id)
                and (category is None or item["category"] == category)
            ]
        if not samples:
            return {
                "status": "NO_METRICS",
                "metrics": {
                    "sample_count": 0, "success_count": 0, "failure_count": 0,
                    "timeout_count": 0, "success_rate": None,
                    "avg_latency_ms": None, "p50_latency_ms": None,
                    "p95_latency_ms": None, "avg_queue_wait_ms": None,
                },
            }
        latencies = [item["latency_ms"] for item in samples]
        waits = [item["queue_wait_ms"] for item in samples]
        success_count = sum(item["success"] for item in samples)
        metrics = {
            "sample_count": len(samples),
            "success_count": success_count,
            "failure_count": len(samples) - success_count,
            "timeout_count": sum(item["timed_out"] for item in samples),
            "success_rate": success_count / len(samples),
            "avg_latency_ms": sum(latencies) / len(latencies),
            "p50_latency_ms": self._percentile(latencies, 0.50),
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "avg_queue_wait_ms": sum(waits) / len(waits),
        }
        return {"status": "METRICS_AVAILABLE", "metrics": metrics}
