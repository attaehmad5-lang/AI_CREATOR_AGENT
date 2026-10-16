"""Bounded provider/model benchmark sample collection and comparison."""
from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any

from .safe_numbers import finite_number


class ProviderBenchmarkManager:
    def __init__(self, *, max_samples: int = 5000) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 100000:
            raise ValueError("max_samples is outside safe limits")
        self.max_samples = max_samples
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._lock = threading.RLock()

    def record(
        self, *, provider_id: str, model_id: str, modality: str, run_id: str,
        success: bool, latency_ms: float, actual_cost: float,
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        for value, name, limit in (
            (provider_id, "provider_id", 128),
            (model_id, "model_id", 256),
            (modality, "modality", 64),
            (run_id, "run_id", 200),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        cost = finite_number(actual_cost, minimum=0)
        quality = None if quality_score is None else finite_number(quality_score, minimum=0)
        if latency is None or cost is None or (quality_score is not None and (quality is None or quality > 1)):
            return {"status": "INVALID_BENCHMARK_METRIC", "saved": False}
        sample = {
            "provider_id": provider_id, "model_id": model_id, "modality": modality,
            "run_id": run_id, "success": success, "latency_ms": latency,
            "actual_cost": cost, "quality_score": quality,
        }
        with self._lock:
            self._samples.append(sample)
        return {"status": "BENCHMARK_SAMPLE_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        return values[max(0, math.ceil(p * len(values)) - 1)]

    def compare(self, *, modality: str | None = None) -> dict[str, Any]:
        with self._lock:
            samples = [dict(s) for s in self._samples if modality is None or s["modality"] == modality]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for sample in samples:
            groups.setdefault((sample["provider_id"], sample["model_id"], sample["modality"]), []).append(sample)
        results = []
        for (provider_id, model_id, sample_modality), group in groups.items():
            successes = [s for s in group if s["success"]]
            latencies = [s["latency_ms"] for s in group]
            costs = [s["actual_cost"] for s in group]
            qualities = [s["quality_score"] for s in group if s["quality_score"] is not None]
            results.append({
                "provider_id": provider_id, "model_id": model_id, "modality": sample_modality,
                "sample_count": len(group), "success_count": len(successes),
                "success_rate": len(successes) / len(group),
                "p50_latency_ms": self._percentile(latencies, 0.50),
                "p95_latency_ms": self._percentile(latencies, 0.95),
                "avg_cost_per_attempt": sum(costs) / len(costs),
                "avg_cost_per_success": (
                    sum(s["actual_cost"] for s in successes) / len(successes)
                    if successes else None
                ),
                "avg_quality_score": sum(qualities) / len(qualities) if qualities else None,
            })
        results.sort(key=lambda r: (
            -r["success_rate"],
            -(r["avg_quality_score"] if r["avg_quality_score"] is not None else -1.0),
            r["p95_latency_ms"] if r["p95_latency_ms"] is not None else float("inf"),
            r["avg_cost_per_success"] if r["avg_cost_per_success"] is not None else float("inf"),
            r["provider_id"], r["model_id"],
        ))
        return {"status": "BENCHMARKS_AVAILABLE" if results else "NO_BENCHMARKS", "results": results}
