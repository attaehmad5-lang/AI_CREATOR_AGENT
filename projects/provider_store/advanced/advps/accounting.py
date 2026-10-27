"""Cost estimation, quotas, budgets, paid-usage approval, idempotency and reconciliation."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from .errors import (
    BudgetExceededError, PaidUsageNotApprovedError, PriceUnknownError, QuotaExceededError, ValidationError,
)
from .storage import AtomicJsonStore


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValidationError(f"{field} must be a finite non-negative number")
    return number


def estimate_cost(model: dict, units: float) -> float:
    """Cost from a catalog model record. Raises PriceUnknownError rather than guessing a price."""
    units = _finite_nonnegative(units, "units")
    price = model.get("price_per_unit")
    if price is None:
        raise PriceUnknownError("model has no known price")
    price = _finite_nonnegative(price, "price_per_unit")
    result = price * units
    if not math.isfinite(result):
        raise ValidationError("estimated cost overflow")
    return round(result, 10)


def _empty() -> dict:
    return {"usage": [], "approved": {}, "budget": None, "quota": {}, "counts": {}, "idem": {}, "alerted": []}


class Accounting:
    def __init__(self, store: AtomicJsonStore, clock: Callable[[], float] = time.time):
        self.store = store
        self.store.default = _empty
        self._clock = clock

    def set_budget(self, limit: float) -> None:
        limit = _finite_nonnegative(limit, "budget")
        self.store.update(lambda s: s.__setitem__("budget", limit))

    def set_quota(self, provider: str, max_requests_per_day: int) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValidationError("provider must be a non-empty string")
        if isinstance(max_requests_per_day, bool) or not isinstance(max_requests_per_day, int) or max_requests_per_day < 0:
            raise ValidationError("quota must be a non-negative integer")
        self.store.update(lambda s: s["quota"].__setitem__(provider, max_requests_per_day))

    def approve_paid(self, provider: str, max_total: float) -> None:
        """Explicit approval to spend up to max_total with a provider (cost > 0)."""
        if not isinstance(provider, str) or not provider.strip():
            raise ValidationError("provider must be a non-empty string")
        max_total = _finite_nonnegative(max_total, "approval")
        self.store.update(lambda s: s["approved"].__setitem__(provider, max_total))

    def revoke_paid(self, provider: str) -> None:
        def op(state):
            state["approved"].pop(provider, None)
        self.store.update(op)

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def total_spend(self, provider: Optional[str] = None) -> float:
        rows = self.store.read()["usage"]
        return round(sum(r["cost"] for r in rows if provider in (None, r["provider"])), 10)

    def reserve_and_record(self, provider: str, model: str, units: float, cost: float, key: Optional[str] = None) -> dict:
        """Atomically check quota/budget/approval and record usage. Idempotent per key."""
        if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
            raise ValidationError("provider and model must be non-empty strings")
        units = _finite_nonnegative(units, "units")
        cost = _finite_nonnegative(cost, "cost")
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise ValidationError("idempotency key must be a non-empty string")
        result: dict[str, Any] = {}

        def op(state):
            if key and key in state["idem"]:
                result.update(state["idem"][key], duplicate=True)
                return None
            day = self._day()
            slot = f"{provider}:{day}"
            quota = state["quota"].get(provider)
            if quota is not None and state["counts"].get(slot, 0) >= quota:
                raise QuotaExceededError(f"daily quota reached for {provider}")
            spent = sum(r["cost"] for r in state["usage"])
            if cost > 0:
                approved = state["approved"].get(provider)
                spent_p = sum(r["cost"] for r in state["usage"] if r["provider"] == provider)
                if approved is None or spent_p + cost > approved + 1e-12:
                    raise PaidUsageNotApprovedError(f"paid usage not approved for {provider}")
            budget = state["budget"]
            if budget is not None and spent + cost > budget + 1e-12:
                raise BudgetExceededError("budget would be exceeded")
            row = {"ts": self._clock(), "provider": provider, "model": model, "units": units, "cost": cost}
            state["usage"].append(row)
            state["counts"][slot] = state["counts"].get(slot, 0) + 1
            alerts = []
            if budget:
                for pct in (0.8, 1.0):
                    tag = f"{pct}"
                    if spent + cost >= budget * pct - 1e-12 and tag not in state["alerted"]:
                        state["alerted"].append(tag)
                        alerts.append(pct)
            result.update(row, duplicate=False, alerts=alerts)
            if key:
                state["idem"][key] = dict(row)
            return None

        self.store.update(op)
        return result

    def reconcile(self, provider: str, reported_total: float, tolerance: float = 1e-6) -> dict:
        reported_total = _finite_nonnegative(reported_total, "reported_total")
        tolerance = _finite_nonnegative(tolerance, "tolerance")
        local = self.total_spend(provider)
        delta = round(reported_total - local, 10)
        return {"provider": provider, "local": local, "reported": reported_total, "delta": delta,
                "discrepancy": abs(delta) > tolerance}
