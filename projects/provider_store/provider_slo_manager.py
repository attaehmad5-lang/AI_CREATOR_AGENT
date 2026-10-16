"""Bounded service-level objective definitions and evaluation for provider metrics."""
from __future__ import annotations

import math
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


class ProviderSLOManager:
    """Evaluate configured targets against the bounded metrics snapshot.

    This is local, sample-window SLO tracking; it is not an external telemetry/SLO platform.
    """

    def __init__(
        self,
        metrics_manager: Any,
        *,
        max_history: int = 500,
        clock: Any = None,
    ) -> None:
        if not callable(getattr(metrics_manager, "snapshot", None)):
            raise ValueError("metrics_manager must provide snapshot()")
        if isinstance(max_history, bool) or not isinstance(max_history, int) or not 1 <= max_history <= 10000:
            raise ValueError("max_history is outside safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.metrics_manager = metrics_manager
        self.max_history = max_history
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._targets: dict[tuple[str, str], dict[str, Any]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)

    @staticmethod
    def _valid_id(value: Any, *, maximum: int) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0," + str(maximum - 1) + r"}", value))

    def set_target(
        self,
        *,
        provider_id: str,
        category: str,
        min_samples: int = 20,
        min_success_rate: float = 0.99,
        max_p95_latency_ms: float | None = None,
        max_timeout_rate: float = 0.05,
    ) -> dict[str, Any]:
        if not self._valid_id(provider_id, maximum=128):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not self._valid_id(category, maximum=64):
            return {"status": "INVALID_CATEGORY", "saved": False}
        if isinstance(min_samples, bool) or not isinstance(min_samples, int) or not 1 <= min_samples <= 100000:
            return {"status": "INVALID_MIN_SAMPLES", "saved": False}
        if (
            not isinstance(min_success_rate, (int, float))
            or isinstance(min_success_rate, bool)
            or not math.isfinite(min_success_rate)
            or not 0 <= min_success_rate <= 1
        ):
            return {"status": "INVALID_SUCCESS_RATE", "saved": False}
        if (
            not isinstance(max_timeout_rate, (int, float))
            or isinstance(max_timeout_rate, bool)
            or not math.isfinite(max_timeout_rate)
            or not 0 <= max_timeout_rate <= 1
        ):
            return {"status": "INVALID_TIMEOUT_RATE", "saved": False}
        if max_p95_latency_ms is not None and (
            not isinstance(max_p95_latency_ms, (int, float))
            or isinstance(max_p95_latency_ms, bool)
            or not math.isfinite(max_p95_latency_ms)
            or max_p95_latency_ms <= 0
        ):
            return {"status": "INVALID_P95_LATENCY", "saved": False}
        target = {
            "provider_id": provider_id,
            "category": category,
            "min_samples": min_samples,
            "min_success_rate": float(min_success_rate),
            "max_p95_latency_ms": float(max_p95_latency_ms) if max_p95_latency_ms is not None else None,
            "max_timeout_rate": float(max_timeout_rate),
        }
        with self._lock:
            self._targets[(provider_id, category)] = target
        return {"status": "SLO_TARGET_SAVED", "saved": True, "target": dict(target)}

    def get_target(self, *, provider_id: str, category: str) -> dict[str, Any]:
        with self._lock:
            target = self._targets.get((provider_id, category))
            return {
                "status": "SLO_TARGET_FOUND" if target else "SLO_TARGET_NOT_FOUND",
                "target": dict(target) if target else None,
            }

    def evaluate(self, *, provider_id: str, category: str) -> dict[str, Any]:
        with self._lock:
            target = self._targets.get((provider_id, category))
            target = dict(target) if target else None
        if target is None:
            return {"status": "SLO_TARGET_NOT_FOUND", "evaluated": False}
        try:
            snapshot = self.metrics_manager.snapshot(provider_id=provider_id, category=category)
        except Exception as exc:
            return {"status": "SLO_METRICS_LOOKUP_FAILED", "evaluated": False, "error_type": type(exc).__name__}
        if not isinstance(snapshot, dict) or snapshot.get("status") not in {"METRICS_AVAILABLE", "NO_METRICS"}:
            return {"status": "SLO_METRICS_INVALID", "evaluated": False}
        metrics = snapshot.get("metrics")
        if not isinstance(metrics, dict):
            return {"status": "SLO_METRICS_INVALID", "evaluated": False}
        count = metrics.get("sample_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < target["min_samples"]:
            return {
                "status": "SLO_INSUFFICIENT_DATA",
                "evaluated": False,
                "sample_count": count if isinstance(count, int) and not isinstance(count, bool) else 0,
                "required_samples": target["min_samples"],
            }
        success_rate = metrics.get("success_rate")
        p95 = metrics.get("p95_latency_ms")
        timeout_count = metrics.get("timeout_count")
        if (
            not isinstance(success_rate, (int, float)) or isinstance(success_rate, bool)
            or not math.isfinite(success_rate) or not 0 <= success_rate <= 1
            or isinstance(timeout_count, bool) or not isinstance(timeout_count, int)
            or not 0 <= timeout_count <= count
            or (p95 is not None and (
                not isinstance(p95, (int, float)) or isinstance(p95, bool)
                or not math.isfinite(p95) or p95 < 0
            ))
        ):
            return {"status": "SLO_METRICS_INVALID", "evaluated": False}
        timeout_rate = timeout_count / count
        breached = []
        if success_rate < target["min_success_rate"]:
            breached.append("SUCCESS_RATE")
        if timeout_rate > target["max_timeout_rate"]:
            breached.append("TIMEOUT_RATE")
        if target["max_p95_latency_ms"] is not None:
            if p95 is None:
                return {"status": "SLO_METRICS_INCOMPLETE", "evaluated": False}
            if p95 > target["max_p95_latency_ms"]:
                breached.append("P95_LATENCY")
        result = {
            "status": "SLO_BREACHED" if breached else "SLO_MET",
            "evaluated": True,
            "provider_id": provider_id,
            "category": category,
            "sample_count": count,
            "success_rate": float(success_rate),
            "timeout_rate": timeout_rate,
            "p95_latency_ms": float(p95) if p95 is not None else None,
            "breached_objectives": breached,
            "target": target,
            "evaluated_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        # Persist only bounded aggregate metadata, never request payloads or credentials.
        history_record = {
            key: result[key]
            for key in (
                "provider_id", "category", "sample_count", "success_rate",
                "timeout_rate", "p95_latency_ms", "breached_objectives",
                "status", "evaluated_at",
            )
        }
        with self._lock:
            self._history.append(history_record)
        return result

    def history(self, *, provider_id: str | None = None, category: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item)
                for item in self._history
                if (provider_id is None or item["provider_id"] == provider_id)
                and (category is None or item["category"] == category)
            ]
