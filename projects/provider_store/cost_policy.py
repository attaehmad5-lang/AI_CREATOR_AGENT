"""Cost, free-credit, quota, and paid-approval guardrails for provider requests.

This module never purchases credits or changes subscriptions. It only decides whether
an explicitly requested operation is eligible to run.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ProviderCostPolicy:
    def __init__(
        self,
        ledger_path: str | os.PathLike[str] | None = None,
        *,
        project_budgets: dict[str, float] | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_path) if ledger_path else None
        if project_budgets is not None and not isinstance(project_budgets, dict):
            raise ValueError("project_budgets must be a mapping")
        self.project_budgets = dict(project_budgets or {})
        for project_id, budget in self.project_budgets.items():
            if not isinstance(project_id, str) or not project_id.strip() or not self._valid_amount(budget):
                raise ValueError("project budgets require non-empty project IDs and finite non-negative amounts")
        self.ledger: dict[str, Any] = {"version": "1.0", "projects": {}}
        if self.ledger_path and self.ledger_path.exists():
            try:
                with self.ledger_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("Invalid usage ledger JSON") from exc
            if not isinstance(loaded, dict) or not isinstance(loaded.get("projects", {}), dict):
                raise ValueError("Invalid usage ledger schema")
            for project_id, project in loaded.get("projects", {}).items():
                if not isinstance(project_id, str) or not project_id.strip() or not isinstance(project, dict):
                    raise ValueError("Invalid usage ledger project record")
                if not self._valid_amount(project.get("actual_spend", 0.0)):
                    raise ValueError("Invalid usage ledger actual_spend")
                requests = project.get("requests", {})
                if not isinstance(requests, dict):
                    raise ValueError("Invalid usage ledger requests")
                if any(not isinstance(request_id, str) or not isinstance(record, dict) for request_id, record in requests.items()):
                    raise ValueError("Invalid usage ledger request record")
            self.ledger = loaded
            self.ledger.setdefault("projects", {})

    @staticmethod
    def _valid_amount(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0

    def _spent(self, project_id: str) -> float:
        project = self.ledger["projects"].get(project_id, {})
        return float(project.get("actual_spend", 0.0)) if isinstance(project, dict) else 0.0

    def evaluate_request(
        self,
        provider: dict[str, Any],
        *,
        project_id: str,
        estimated_cost: float,
        free_credits_remaining: float = 0.0,
        paid_usage_approved: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(provider, dict) or not isinstance(provider.get("id"), str) or not provider["id"]:
            return {"allowed": False, "status": "INVALID_PROVIDER"}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"allowed": False, "status": "INVALID_PROJECT"}
        if not self._valid_amount(estimated_cost) or not self._valid_amount(free_credits_remaining):
            return {"allowed": False, "status": "INVALID_COST_AMOUNT"}
        if not isinstance(paid_usage_approved, bool):
            return {"allowed": False, "status": "INVALID_PAID_APPROVAL_FLAG", "provider": provider["id"]}
        if "enabled" in provider and not isinstance(provider["enabled"], bool):
            return {"allowed": False, "status": "INVALID_PROVIDER_CONFIGURATION", "provider": provider["id"]}
        if provider.get("enabled") is False:
            return {"allowed": False, "status": "PROVIDER_DISABLED", "provider": provider["id"]}

        if estimated_cost == 0:
            return {
                "allowed": True, "status": "ALLOWED_ZERO_COST", "provider": provider["id"],
                "billing_mode": "ZERO_COST", "estimated_cost": 0.0,
            }

        if free_credits_remaining >= estimated_cost:
            return {
                "allowed": True, "status": "ALLOWED_FREE_CREDITS", "provider": provider["id"],
                "billing_mode": "FREE_CREDITS", "estimated_cost": estimated_cost,
                "free_credits_remaining": free_credits_remaining,
            }

        budget = self.project_budgets.get(project_id)
        if budget is None:
            return {
                "allowed": False, "status": "PROJECT_BUDGET_REQUIRED",
                "provider": provider["id"], "estimated_cost": estimated_cost,
            }
        if not self._valid_amount(budget):
            return {"allowed": False, "status": "INVALID_PROJECT_BUDGET", "provider": provider["id"]}
        remaining = max(0.0, budget - self._spent(project_id))
        if estimated_cost > remaining:
            return {
                "allowed": False, "status": "PROJECT_BUDGET_EXCEEDED",
                "provider": provider["id"], "estimated_cost": estimated_cost,
                "budget_remaining": remaining,
            }

        # A paid subscription/credit balance is never inferred to be approved.
        if not paid_usage_approved:
            return {
                "allowed": False, "status": "PAID_APPROVAL_REQUIRED", "provider": provider["id"],
                "estimated_cost": estimated_cost,
            }
        return {
            "allowed": True, "status": "ALLOWED_PAID", "provider": provider["id"],
            "billing_mode": "PAID_APPROVED", "estimated_cost": estimated_cost,
            "approval_recorded": True,
        }

    @classmethod
    def _sortable_credit_amount(cls, value: Any) -> float:
        """Return a safe sort key; the policy validator still rejects invalid input."""
        return float(value) if cls._valid_amount(value) else 0.0

    def select_provider(
        self,
        providers: list[dict[str, Any]],
        *,
        project_id: str,
        estimated_costs: dict[str, float],
        free_credits: dict[str, float] | None = None,
        paid_approved_providers: set[str] | None = None,
    ) -> dict[str, Any]:
        """Prefer eligible free-credit providers before any explicitly approved paid route."""
        if not isinstance(providers, list) or not isinstance(estimated_costs, dict):
            return {"allowed": False, "status": "INVALID_SELECTION_INPUT"}
        if free_credits is not None and not isinstance(free_credits, dict):
            return {"allowed": False, "status": "INVALID_FREE_CREDIT_MAP"}
        if paid_approved_providers is not None and not isinstance(paid_approved_providers, (set, frozenset)):
            return {"allowed": False, "status": "INVALID_PAID_APPROVAL_SET"}
        free_credits = {} if free_credits is None else free_credits
        paid_approved_providers = set() if paid_approved_providers is None else paid_approved_providers
        candidates = sorted(
            (p for p in providers if isinstance(p, dict)),
            key=lambda p: (
                0 if self._sortable_credit_amount(free_credits.get(str(p.get("id")), 0)) > 0 else 1,
                -self._sortable_credit_amount(free_credits.get(str(p.get("id")), 0)),
                str(p.get("id", "")),
            ),
        )
        decisions = []
        for provider in candidates:
            provider_id = str(provider.get("id", ""))
            estimate = estimated_costs.get(provider_id)
            if estimate is None:
                decisions.append({"provider": provider_id, "status": "ESTIMATE_MISSING"})
                continue
            decision = self.evaluate_request(
                provider,
                project_id=project_id,
                estimated_cost=estimate,
                free_credits_remaining=free_credits.get(provider_id, 0.0),
                paid_usage_approved=provider_id in paid_approved_providers,
            )
            if decision["allowed"]:
                return {**decision, "selection_order": [d.get("provider") for d in decisions] + [provider_id]}
            decisions.append({"provider": provider_id, "status": decision["status"]})
        return {"allowed": False, "status": "NO_POLICY_COMPLIANT_PROVIDER", "decisions": decisions}

    def reconcile_usage(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        actual_cost: float,
        request_id: str,
    ) -> dict[str, Any]:
        """Record actual usage idempotently and flag estimate variance/budget breaches."""
        if not all(isinstance(v, str) and v.strip() for v in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not self._valid_amount(estimated_cost) or not self._valid_amount(actual_cost):
            return {"status": "INVALID_COST_AMOUNT", "saved": False}
        projects = self.ledger["projects"]
        project = projects.setdefault(project_id, {"actual_spend": 0.0, "requests": {}})
        requests = project.setdefault("requests", {})
        if request_id in requests:
            return {"status": "DUPLICATE_REQUEST", "saved": False, "request_id": request_id}
        record = {
            "provider_id": provider_id,
            "estimated_cost": float(estimated_cost),
            "actual_cost": float(actual_cost),
            "variance": round(float(actual_cost) - float(estimated_cost), 6),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        requests[request_id] = record
        project["actual_spend"] = round(float(project.get("actual_spend", 0.0)) + float(actual_cost), 6)
        if self.ledger_path:
            self._save()
        budget = self.project_budgets.get(project_id)
        budget_exceeded = budget is not None and project["actual_spend"] > budget
        return {
            "status": "BUDGET_EXCEEDED" if budget_exceeded else "RECONCILED",
            "saved": True,
            "project_id": project_id,
            "request_id": request_id,
            "actual_spend": project["actual_spend"],
            "budget": budget,
            "budget_exceeded": budget_exceeded,
            "variance": record["variance"],
        }

    def _save(self) -> None:
        assert self.ledger_path is not None
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.ledger_path.name}.", suffix=".tmp",
            dir=str(self.ledger_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.ledger, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.ledger_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
