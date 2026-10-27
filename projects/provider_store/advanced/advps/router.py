"""Capability/cost/latency/region routing with circuit breakers, retries and ordered fallback."""
from __future__ import annotations

import itertools
import threading
from typing import Any, Callable, Optional

from .catalog import Catalog
from .errors import AdvpsError, CircuitOpenError, NoProviderError, ProviderError
from .redact import redact_text
from .resilience import CircuitBreaker, RetryBudget, RetryPolicy, retry_call

STRATEGIES = ("priority", "cost", "latency", "round_robin")


def _best_model(provider: dict, capability: str) -> Optional[dict]:
    models = [m for m in provider["models"].values() if capability in m["capabilities"]]
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

    def candidates(self, capability: str, strategy: str = "priority", region: Optional[str] = None,
                   exclude: tuple = (), max_price: Optional[float] = None) -> list:
        """Ordered enabled providers able to serve capability. Pure; used by dry-run simulation."""
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        out = []
        for prov in self.catalog.list(capability=capability, enabled_only=True, region=region):
            if prov["id"] in exclude:
                continue
            model = _best_model(prov, capability)
            price = model["price_per_unit"] if model else None
            if max_price is not None and (price is None or price > max_price):
                continue
            out.append((prov, model))
        if strategy == "cost":
            out.sort(key=lambda pm: (pm[1] is None or pm[1]["price_per_unit"] is None,
                                     (pm[1] or {}).get("price_per_unit") or 0.0, pm[0]["priority"], pm[0]["id"]))
        elif strategy == "latency":
            out.sort(key=lambda pm: (pm[1] is None or pm[1]["avg_latency_ms"] is None,
                                     (pm[1] or {}).get("avg_latency_ms") or 0.0, pm[0]["id"]))
        elif strategy == "round_robin" and out:
            k = next(self._rr) % len(out)
            out = out[k:] + out[:k]
        return out

    def simulate(self, capability: str, **kw: Any) -> list:
        return [p["id"] for p, _ in self.candidates(capability, **kw)]

    def execute(self, capability: str, call: Callable[[dict, Optional[dict]], Any], strategy: str = "priority",
                region: Optional[str] = None, idempotent: bool = False, max_price: Optional[float] = None):
        """Try providers in order; returns (provider_id, result). Errors are normalized and redacted."""
        cands = self.candidates(capability, strategy, region, max_price=max_price)
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
