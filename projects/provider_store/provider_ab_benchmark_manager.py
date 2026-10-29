"""Safe, side-effect-free A/B benchmark planning for provider routing."""
from __future__ import annotations

import re
from typing import Any

from .safe_numbers import finite_number

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")


class ProviderABBenchmarkManager:
    """Build benchmark plans only; never invokes providers or spends credits."""

    def __init__(self, *, max_candidates: int = 8, max_repetitions: int = 20) -> None:
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or not 2 <= max_candidates <= 32:
            raise ValueError("max_candidates must be an integer from 2 to 32")
        if isinstance(max_repetitions, bool) or not isinstance(max_repetitions, int) or not 1 <= max_repetitions <= 100:
            raise ValueError("max_repetitions must be an integer from 1 to 100")
        self.max_candidates = max_candidates
        self.max_repetitions = max_repetitions

    def plan(
        self,
        *,
        benchmark_id: str,
        category: str,
        candidates: list[dict[str, Any]],
        repetitions: int = 3,
        allow_paid: bool = False,
        paid_approval: dict[str, Any] | None = None,
        max_total_cost: float = 0.0,
    ) -> dict[str, Any]:
        if not isinstance(benchmark_id, str) or not _ID.fullmatch(benchmark_id):
            return {"status": "INVALID_BENCHMARK_ID", "executable": False}
        if not isinstance(category, str) or not _ID.fullmatch(category):
            return {"status": "INVALID_CATEGORY", "executable": False}
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= self.max_candidates:
            return {"status": "INVALID_CANDIDATE_COUNT", "executable": False}
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 1 <= repetitions <= self.max_repetitions:
            return {"status": "INVALID_REPETITIONS", "executable": False}
        budget = finite_number(max_total_cost, minimum=0)
        if budget is None:
            return {"status": "INVALID_BUDGET", "executable": False}
        if not isinstance(allow_paid, bool):
            return {"status": "INVALID_PAID_USAGE_FLAG", "executable": False}

        seen: set[str] = set()
        normalized = []
        total_estimate = 0.0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                return {"status": "INVALID_CANDIDATE", "executable": False}
            provider_id = candidate.get("provider_id")
            model_id = candidate.get("model_id", "default")
            estimate = finite_number(candidate.get("estimated_cost_per_run", 0), minimum=0)
            if (
                not isinstance(provider_id, str) or not _ID.fullmatch(provider_id)
                or not isinstance(model_id, str) or not _ID.fullmatch(model_id)
                or estimate is None
            ):
                return {"status": "INVALID_CANDIDATE", "executable": False}
            key = f"{provider_id}|{model_id}"
            if key in seen:
                return {"status": "DUPLICATE_CANDIDATE", "executable": False}
            seen.add(key)
            total_estimate += estimate * repetitions
            normalized.append({
                "provider_id": provider_id,
                "model_id": model_id,
                "estimated_cost_per_run": estimate,
            })

        if total_estimate > budget:
            return {
                "status": "BENCHMARK_BUDGET_EXCEEDED",
                "executable": False,
                "estimated_total_cost": round(total_estimate, 8),
                "max_total_cost": budget,
            }
        if total_estimate > 0:
            approval = paid_approval if isinstance(paid_approval, dict) else {}
            if not allow_paid or approval.get("approved") is not True or approval.get("benchmark_id") != benchmark_id:
                return {
                    "status": "PAID_BENCHMARK_APPROVAL_REQUIRED",
                    "executable": False,
                    "estimated_total_cost": round(total_estimate, 8),
                }
            if not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip():
                return {"status": "INVALID_PAID_APPROVAL", "executable": False}

        return {
            "status": "BENCHMARK_PLAN_READY",
            "executable": False,
            "execution_started": False,
            "provider_calls_made": 0,
            "benchmark_id": benchmark_id,
            "category": category,
            "repetitions": repetitions,
            "candidate_count": len(normalized),
            "candidates": normalized,
            "estimated_total_cost": round(total_estimate, 8),
            "max_total_cost": budget,
            "paid_usage_approved": total_estimate > 0,
            "note": "Planning only: execute through a separately authorized runner; this method never calls providers.",
        }
