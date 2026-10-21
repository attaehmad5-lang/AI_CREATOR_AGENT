"""Service-level objective evaluation over bounded provider metric snapshots.

This evaluator is advisory: it does not claim a time-window SLO unless the caller's
metrics source itself represents that window, and it does not dispatch alerts.
"""
from __future__ import annotations

from typing import Any

from .safe_numbers import finite_number


class ProviderSLOManager:
    """Evaluate availability and p95 latency targets against a metrics manager."""

    MAX_POLICIES = 500
    MAX_SAMPLES = 100_000

    def __init__(self, metrics_manager: Any, policies: dict[str, dict[str, Any]]) -> None:
        if not callable(getattr(metrics_manager, "snapshot", None)):
            raise ValueError("metrics_manager must support snapshot")
        if not isinstance(policies, dict) or len(policies) > self.MAX_POLICIES:
            raise ValueError("policies must be a bounded mapping")
        normalized: dict[str, dict[str, Any]] = {}
        for scope, policy in policies.items():
            if not isinstance(scope, str) or not scope.strip() or len(scope) > 128:
                raise ValueError("SLO scope must be a non-empty string of at most 128 characters")
            if not isinstance(policy, dict):
                raise ValueError("SLO policy must be an object")
            target = finite_number(policy.get("target_success_rate"), minimum=0, maximum=1)
            max_p95 = finite_number(policy.get("max_p95_latency_ms"), minimum=0)
            min_samples = policy.get("min_samples", 20)
            if target is None or max_p95 is None or max_p95 <= 0:
                raise ValueError("SLO targets must be finite and within valid ranges")
            if (
                not isinstance(min_samples, int) or isinstance(min_samples, bool)
                or not 1 <= min_samples <= self.MAX_SAMPLES
            ):
                raise ValueError("min_samples must be an integer within safe limits")
            normalized[scope] = {
                "target_success_rate": target,
                "max_p95_latency_ms": max_p95,
                "min_samples": min_samples,
            }
        self.metrics_manager = metrics_manager
        self.policies = normalized

    def evaluate(self, provider_id: str, *, category: str | None = None) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 128:
            return {"status": "INVALID_PROVIDER_ID", "provider": provider_id, "slo_met": False}
        if category is not None and (
            not isinstance(category, str) or not category.strip() or len(category) > 64
        ):
            return {"status": "INVALID_CATEGORY", "provider": provider_id, "slo_met": False}

        scope = provider_id if provider_id in self.policies else (
            f"category:{category}" if category is not None else None
        )
        policy = self.policies.get(scope) if scope is not None else None
        if policy is None:
            return {
                "status": "SLO_POLICY_NOT_FOUND",
                "provider": provider_id,
                "slo_met": False,
            }
        try:
            snapshot = self.metrics_manager.snapshot(provider_id=provider_id, category=category)
        except Exception as exc:
            return {
                "status": "METRICS_LOOKUP_FAILED",
                "provider": provider_id,
                "slo_met": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metrics"), dict):
            return {"status": "INVALID_METRICS_RESPONSE", "provider": provider_id, "slo_met": False}
        metrics = snapshot["metrics"]
        sample_count = metrics.get("sample_count")
        if (
            not isinstance(sample_count, int) or isinstance(sample_count, bool)
            or sample_count < 0 or sample_count > self.MAX_SAMPLES
        ):
            return {"status": "INVALID_METRICS_RESPONSE", "provider": provider_id, "slo_met": False}
        if sample_count < policy["min_samples"]:
            return {
                "status": "INSUFFICIENT_DATA",
                "provider": provider_id,
                "slo_met": False,
                "sample_count": sample_count,
                "required_samples": policy["min_samples"],
                "policy": dict(policy),
            }
        success_rate = finite_number(metrics.get("success_rate"), minimum=0, maximum=1)
        p95 = finite_number(metrics.get("p95_latency_ms"), minimum=0)
        if success_rate is None or p95 is None:
            return {"status": "INVALID_METRICS_RESPONSE", "provider": provider_id, "slo_met": False}
        availability_met = success_rate >= policy["target_success_rate"]
        latency_met = p95 <= policy["max_p95_latency_ms"]
        error_rate = 1.0 - success_rate
        allowed_error_rate = 1.0 - policy["target_success_rate"]
        if allowed_error_rate > 0:
            error_budget_remaining = max(0.0, allowed_error_rate - error_rate) / allowed_error_rate
        else:
            error_budget_remaining = 1.0 if error_rate == 0 else 0.0
        slo_met = availability_met and latency_met
        return {
            "status": "SLO_MET" if slo_met else "SLO_BREACHED",
            "provider": provider_id,
            "category": category,
            "slo_met": slo_met,
            "sample_count": sample_count,
            "availability": {
                "target": policy["target_success_rate"],
                "observed": success_rate,
                "met": availability_met,
            },
            "latency": {
                "p95_target_ms": policy["max_p95_latency_ms"],
                "p95_observed_ms": p95,
                "met": latency_met,
            },
            "error_budget_remaining_fraction": round(error_budget_remaining, 6),
            "policy": dict(policy),
            "note": "Evaluation uses the metrics manager's retained sample set; it is not a time-window guarantee or an alert delivery.",
        }
