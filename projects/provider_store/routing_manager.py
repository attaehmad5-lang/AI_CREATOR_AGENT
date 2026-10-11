"""Policy-aware provider routing and fallback selection.

This module selects providers only; it does not make network calls, spend money,
or claim a provider is live unless health evidence was supplied by the caller.
"""
from __future__ import annotations

import math
import threading
from typing import Any


class ProviderRoutingManager:
    STRATEGIES = {
        "PRIMARY_THEN_FALLBACK",
        "FREE_CREDITS_FIRST",
        "LOWEST_COST",
        "HEALTHIEST_FIRST",
        "ROUND_ROBIN",
    }

    def __init__(
        self,
        connection_manager: Any,
        *,
        health_manager: Any | None = None,
        cost_policy: Any | None = None,
        default_strategy: str = "PRIMARY_THEN_FALLBACK",
    ) -> None:
        if connection_manager is None or not callable(getattr(connection_manager, "discover_providers", None)):
            raise ValueError("connection_manager must support provider discovery")
        if health_manager is not None and not callable(getattr(health_manager, "is_eligible", None)):
            raise ValueError("health_manager must support health eligibility checks")
        if cost_policy is not None and not callable(getattr(cost_policy, "evaluate_request", None)):
            raise ValueError("cost_policy must support request evaluation")
        normalized_strategy = str(default_strategy).upper()
        if normalized_strategy not in self.STRATEGIES:
            raise ValueError("unsupported routing strategy")
        self.connection_manager = connection_manager
        self.health_manager = health_manager
        self.cost_policy = cost_policy
        self.default_strategy = normalized_strategy
        self._round_robin_counters: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _valid_amount(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    @staticmethod
    def _priority(provider: dict[str, Any]) -> tuple[float, str]:
        value = provider.get("priority", 1000)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            value = 1000
        return float(value), str(provider.get("id", ""))

    def route(
        self,
        *,
        category: str,
        project_id: str,
        estimated_cost: float = 0.0,
        capability: str | None = None,
        strategy: str | None = None,
        estimated_costs: dict[str, float] | None = None,
        free_credits: dict[str, float] | None = None,
        paid_approved_providers: set[str] | frozenset[str] | None = None,
        excluded_providers: set[str] | frozenset[str] | None = None,
        require_health_evidence: bool = False,
    ) -> dict[str, Any]:
        """Return the first policy-compliant provider and the decisions that led to it."""
        if not isinstance(category, str) or not category.strip():
            return {"allowed": False, "status": "INVALID_ROUTE_REQUEST", "reason": "CATEGORY_REQUIRED"}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"allowed": False, "status": "INVALID_ROUTE_REQUEST", "reason": "PROJECT_REQUIRED"}
        if not self._valid_amount(estimated_cost):
            return {"allowed": False, "status": "INVALID_ROUTE_REQUEST", "reason": "INVALID_ESTIMATED_COST"}
        selected_strategy = str(strategy or self.default_strategy).upper()
        if selected_strategy not in self.STRATEGIES:
            return {"allowed": False, "status": "INVALID_ROUTING_STRATEGY"}
        if estimated_costs is not None and not isinstance(estimated_costs, dict):
            return {"allowed": False, "status": "INVALID_COST_ESTIMATES"}
        if free_credits is not None and not isinstance(free_credits, dict):
            return {"allowed": False, "status": "INVALID_FREE_CREDIT_MAP"}
        if paid_approved_providers is not None and not isinstance(paid_approved_providers, (set, frozenset)):
            return {"allowed": False, "status": "INVALID_PAID_APPROVAL_SET"}
        if excluded_providers is not None and not isinstance(excluded_providers, (set, frozenset)):
            return {"allowed": False, "status": "INVALID_EXCLUDED_PROVIDER_SET"}
        if not isinstance(require_health_evidence, bool):
            return {"allowed": False, "status": "INVALID_HEALTH_POLICY"}

        estimates = {} if estimated_costs is None else estimated_costs
        credits = {} if free_credits is None else free_credits
        approvals = set() if paid_approved_providers is None else paid_approved_providers
        excluded = set() if excluded_providers is None else excluded_providers
        if any(not isinstance(key, str) or not self._valid_amount(value) for key, value in estimates.items()):
            return {"allowed": False, "status": "INVALID_COST_ESTIMATES"}
        if any(not isinstance(key, str) or not self._valid_amount(value) for key, value in credits.items()):
            return {"allowed": False, "status": "INVALID_FREE_CREDIT_MAP"}
        if any(not isinstance(value, str) for value in approvals):
            return {"allowed": False, "status": "INVALID_PAID_APPROVAL_SET"}
        if any(not isinstance(value, str) for value in excluded):
            return {"allowed": False, "status": "INVALID_EXCLUDED_PROVIDER_SET"}

        if (selected_strategy == "HEALTHIEST_FIRST" or require_health_evidence) and self.health_manager is None:
            return {"allowed": False, "status": "HEALTH_DATA_REQUIRED"}

        try:
            discovered = self.connection_manager.discover_providers(
                category=category.strip(),
                capability=capability,
                enabled_only=True,
            )
        except Exception as exc:
            return {
                "allowed": False,
                "status": "PROVIDER_DISCOVERY_FAILED",
                "error_type": type(exc).__name__,
            }

        candidates: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for provider in discovered:
            if not isinstance(provider, dict):
                continue
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or not provider_id or provider_id in excluded:
                continue
            if provider.get("enabled") is not True:
                continue
            health_record: dict[str, Any] = {}
            if self.health_manager is not None:
                health = self.health_manager.is_eligible(provider_id)
                if not isinstance(health, dict) or health.get("eligible") is not True:
                    decisions.append({
                        "provider": provider_id,
                        "status": health.get("status", "HEALTH_UNKNOWN") if isinstance(health, dict) else "HEALTH_UNKNOWN",
                    })
                    continue
                raw_health = getattr(self.health_manager, "data", {}).get("providers", {}).get(provider_id, {})
                if isinstance(raw_health, dict):
                    health_record = raw_health
            cost = estimates.get(provider_id, estimated_cost)
            if not self._valid_amount(cost):
                decisions.append({"provider": provider_id, "status": "INVALID_COST_AMOUNT"})
                continue
            credit = credits.get(provider_id, 0.0)
            credit_sort = float(credit) if self._valid_amount(credit) else 0.0
            candidates.append({
                **provider,
                "_route_cost": float(cost),
                "_route_credits": credit_sort,
                "_route_latency": health_record.get("last_latency_ms", float("inf")),
            })

        if selected_strategy == "LOWEST_COST":
            candidates.sort(key=lambda p: (p["_route_cost"], -p["_route_credits"], *self._priority(p)))
        elif selected_strategy == "FREE_CREDITS_FIRST":
            candidates.sort(key=lambda p: (-p["_route_credits"], *self._priority(p)))
        elif selected_strategy == "HEALTHIEST_FIRST":
            candidates.sort(key=lambda p: (p["_route_latency"], *self._priority(p)))
        elif selected_strategy == "ROUND_ROBIN" and candidates:
            candidates.sort(key=self._priority)
            with self._lock:
                start = self._round_robin_counters.get(category.strip().casefold(), 0) % len(candidates)
                self._round_robin_counters[category.strip().casefold()] = start + 1
            candidates = candidates[start:] + candidates[:start]
        else:
            candidates.sort(key=self._priority)

        for provider in candidates:
            provider_id = provider["id"]
            cost = provider["_route_cost"]
            credit = credits.get(provider_id, 0.0)
            if self.cost_policy is not None:
                decision = self.cost_policy.evaluate_request(
                    provider,
                    project_id=project_id,
                    estimated_cost=cost,
                    free_credits_remaining=credit,
                    paid_usage_approved=provider_id in approvals,
                )
                if not decision.get("allowed"):
                    decisions.append({"provider": provider_id, "status": decision.get("status", "POLICY_REJECTED")})
                    continue
                return {
                    "allowed": True,
                    "status": "ROUTED",
                    "provider": provider_id,
                    "category": category.strip(),
                    "strategy": selected_strategy,
                    "billing_mode": decision.get("billing_mode"),
                    "estimated_cost": cost,
                    "health_verified": self.health_manager is not None,
                    "prior_decisions": decisions,
                }
            return {
                "allowed": True,
                "status": "ROUTED",
                "provider": provider_id,
                "category": category.strip(),
                "strategy": selected_strategy,
                "estimated_cost": cost,
                "health_verified": self.health_manager is not None,
                "prior_decisions": decisions,
            }

        return {
            "allowed": False,
            "status": "NO_POLICY_COMPLIANT_PROVIDER",
            "category": category.strip(),
            "strategy": selected_strategy,
            "decisions": decisions,
        }

    def select_fallback(self, *, failed_provider: str, **route_options: Any) -> dict[str, Any]:
        """Route around a failed provider without retrying that same provider."""
        if not isinstance(failed_provider, str) or not failed_provider.strip():
            return {"allowed": False, "status": "INVALID_FAILED_PROVIDER"}
        excluded = route_options.get("excluded_providers", frozenset())
        if not isinstance(excluded, (set, frozenset)):
            return {"allowed": False, "status": "INVALID_EXCLUDED_PROVIDER_SET"}
        route_options["excluded_providers"] = set(excluded) | {failed_provider}
        result = self.route(**route_options)
        result["failed_provider"] = failed_provider
        return result
