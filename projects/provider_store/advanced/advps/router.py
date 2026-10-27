"""Capability/cost/latency/quality/media/region routing with retries and ordered fallback."""
from __future__ import annotations

import itertools
import math
import threading
from typing import Any, Callable, Optional

from .catalog import Catalog, MEDIA_TYPES
from .errors import AdvpsError, CircuitOpenError, NoProviderError, ProviderError, ValidationError
from .redact import redact_text
from .resilience import CircuitBreaker, RetryBudget, RetryPolicy, retry_call

STRATEGIES = ("priority", "cost", "latency", "quality", "round_robin")


def _best_model(
    provider: dict, capability: str, media_type: Optional[str] = None,
    min_quality_score: Optional[float] = None,
) -> Optional[dict]:
    models = [m for m in provider["models"].values() if capability in m["capabilities"]]
    if media_type is not None:
        models = [m for m in models if media_type in m.get("media_types", [])]
    if min_quality_score is not None:
        models = [m for m in models if m.get("quality_score") is not None
                  and m["quality_score"] >= min_quality_score]
    if not models:
        return None
    priced = [m for m in models if m["price_per_unit"] is not None]
    return min(priced, key=lambda m: m["price_per_unit"]) if priced else models[0]


class Router:
    def __init__(self, catalog: Catalog, retry_policy: Optional[RetryPolicy] = None,
                 breaker_factory: Optional[Callable[[], CircuitBreaker]] = None, sleep=None):
        self.catalog = catalog
        self.retry_policy = retry_policy or RetryPolicy()
        self._breaker_factory = breaker_factory or CircuitBreaker
        self._breakers: dict = {}
        self._budgets: dict = {}
        self._rr = itertools.count()
        self._lock = threading.Lock()
        self._sleep = sleep

    def breaker(self, pid: str) -> CircuitBreaker:
        with self._lock:
            return self._breakers.setdefault(pid, self._breaker_factory())

    def _budget(self, pid: str) -> RetryBudget:
        with self._lock:
            return self._budgets.setdefault(pid, RetryBudget())

    def candidates(
        self, capability: str, strategy: str = "priority", region: Optional[str] = None,
        exclude: tuple = (), max_price: Optional[float] = None,
        min_quality_score: Optional[float] = None, media_type: Optional[str] = None,
    ) -> list:
        """Ordered enabled providers meeting capability and optional media/quality/budget policy."""
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        if max_price is not None and (
            isinstance(max_price, bool) or not isinstance(max_price, (int, float))
            or not math.isfinite(max_price) or max_price < 0
        ):
            raise ValidationError("max_price must be finite and non-negative")
        if min_quality_score is not None and (
            isinstance(min_quality_score, bool) or not isinstance(min_quality_score, (int, float))
            or not math.isfinite(min_quality_score) or not 0 <= min_quality_score <= 100
        ):
            raise ValidationError("min_quality_score must be between 0 and 100")
        if media_type is not None and media_type not in MEDIA_TYPES:
            raise ValidationError(f"unsupported media_type: {media_type}")
        out = []
        for prov in self.catalog.list(capability=capability, enabled_only=True, region=region):
            if prov["id"] in exclude:
                continue
            model = _best_model(prov, capability, media_type, min_quality_score)
            if model is None:
                continue
            price = model["price_per_unit"]
            if max_price is not None and (price is None or price > max_price):
                continue
            out.append((prov, model))
        if strategy == "cost":
            out.sort(key=lambda pm: (pm[1]["price_per_unit"] is None,
                                     pm[1]["price_per_unit"] if pm[1]["price_per_unit"] is not None else float("inf"),
                                     pm[0]["priority"], pm[0]["id"]))
        elif strategy == "latency":
            out.sort(key=lambda pm: (pm[1]["avg_latency_ms"] is None,
                                     pm[1]["avg_latency_ms"] if pm[1]["avg_latency_ms"] is not None else float("inf"),
                                     pm[0]["id"]))
        elif strategy == "quality":
            out.sort(key=lambda pm: (
                pm[1]["quality_score"] is None,
                -(pm[1]["quality_score"] if pm[1]["quality_score"] is not None else -1),
                pm[0]["priority"], pm[0]["id"],
            ))
        elif strategy == "round_robin" and out:
            k = next(self._rr) % len(out)
            out = out[k:] + out[:k]
        return out

    def simulate(self, capability: str, **kw: Any) -> list:
        return [p["id"] for p, _ in self.candidates(capability, **kw)]

    def execute(
        self, capability: str, call: Callable[[dict, Optional[dict]], Any], strategy: str = "priority",
        region: Optional[str] = None, idempotent: bool = False, max_price: Optional[float] = None,
        min_quality_score: Optional[float] = None, media_type: Optional[str] = None,
    ):
        """Try eligible providers in order; returns (provider_id, result)."""
        cands = self.candidates(
            capability, strategy, region, max_price=max_price,
            min_quality_score=min_quality_score, media_type=media_type,
        )
        if not cands:
            raise NoProviderError(f"no enabled provider for capability: {capability}")
        failures = []
        for prov, model in cands:
            pid = prov["id"]
            brk = self.breaker(pid)
            try:
                brk.allow()
            except CircuitOpenError:
                failures.append((pid, "circuit_open"))
                continue
            try:
                kwargs = {"sleep": self._sleep} if self._sleep else {}
                result = retry_call(lambda: call(prov, model), self.retry_policy, self._budget(pid),
                                    idempotent=idempotent, **kwargs)
            except ProviderError as exc:
                brk.record_failure()
                failures.append((pid, exc.code))
                if exc.code == "ambiguous_outcome" and not idempotent:
                    raise ProviderError(redact_text(f"{pid}: ambiguous outcome; not retried or failed over"),
                                        retryable=False) from exc
                continue
            except AdvpsError:
                raise
            brk.record_success()
            return pid, result
        raise NoProviderError("all providers failed: " + ", ".join(f"{p}={c}" for p, c in failures))
