"""Cost, free-credit, quota, and paid-approval guardrails for provider requests.

This module never purchases credits or changes subscriptions. It only decides whether
an explicitly requested operation is eligible to run.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


class ProviderCostPolicy:
    def __init__(
        self,
        ledger_path: str | os.PathLike[str] | None = None,
        *,
        project_budgets: dict[str, float] | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_path) if ledger_path else None
        self._lock = threading.RLock()
        if project_budgets is not None and not isinstance(project_budgets, dict):
            raise ValueError("project_budgets must be a mapping")
        self.project_budgets = dict(project_budgets or {})
        for project_id, budget in self.project_budgets.items():
            if not isinstance(project_id, str) or not project_id.strip() or not self._valid_amount(budget):
                raise ValueError("project budgets require non-empty project IDs and finite non-negative amounts")
        self.ledger: dict[str, Any] = {"version": "1.0", "projects": {}}
        # Constructor reads share the writer lock to avoid Windows replacement races.
        with self._transaction():
            pass

    def _reload_ledger(self) -> None:
        """Reload persistent usage state after acquiring the interprocess lock."""
        if self.ledger_path is None or not self.ledger_path.exists():
            self.ledger = {"version": "1.0", "projects": {}}
            return
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
            if any(not isinstance(request_id, str) or not request_id.strip() or not isinstance(record, dict) for request_id, record in requests.items()):
                raise ValueError("Invalid usage ledger request record")
            actual_cost_total = 0.0
            for request_id, record in requests.items():
                if (
                    not isinstance(record.get("provider_id"), str) or not record["provider_id"].strip()
                    or not self._valid_amount(record.get("estimated_cost"))
                    or not self._valid_amount(record.get("actual_cost"))
                ):
                    raise ValueError("Invalid usage ledger request amounts or provider")
                if "execution_success" in record and not isinstance(record["execution_success"], bool):
                    raise ValueError("Invalid usage ledger execution outcome")
                if "charge_outcome" in record and record["charge_outcome"] not in {"unknown", "incurred", "not_incurred"}:
                    raise ValueError("Invalid usage ledger charge outcome")
                if record.get("actual_cost", 0.0) > 0 and record.get("charge_outcome") == "not_incurred":
                    raise ValueError("Inconsistent usage ledger charge outcome")
                try:
                    recorded_at = record.get("recorded_at")
                    if not isinstance(recorded_at, str):
                        raise ValueError("missing timestamp")
                    datetime.fromisoformat(recorded_at)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid usage ledger record timestamp") from exc
                actual_cost_total += float(record["actual_cost"])
            if not math.isclose(
                float(project.get("actual_spend", 0.0)), actual_cost_total,
                rel_tol=0.0, abs_tol=max(1e-6, len(requests) * 1e-6),
            ):
                raise ValueError("Usage ledger spend does not match its request records")
            reservations = project.get("reservations", {})
            if not isinstance(reservations, dict):
                raise ValueError("Invalid usage ledger reservations")
            if any(
                not isinstance(request_id, str) or not request_id.strip()
                or not isinstance(record, dict)
                or not isinstance(record.get("provider_id"), str) or not record["provider_id"].strip()
                or not self._valid_amount(record.get("estimated_cost"))
                for request_id, record in reservations.items()
            ):
                raise ValueError("Invalid usage ledger reservation record")
            for record in reservations.values():
                try:
                    reserved_at = record.get("reserved_at")
                    if not isinstance(reserved_at, str):
                        raise ValueError("missing timestamp")
                    datetime.fromisoformat(reserved_at)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid usage ledger reservation timestamp") from exc
        self.ledger = loaded
        self.ledger.setdefault("projects", {})

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize ledger operations across threads and processes when persistence is enabled."""
        with self._lock:
            if self.ledger_path is None:
                yield
                return
            lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_ledger()
                yield

    @staticmethod
    def _valid_amount(value: Any) -> bool:
        return finite_number(value, minimum=0) is not None

    def _spent(self, project_id: str) -> float:
        project = self.ledger["projects"].get(project_id, {})
        return float(project.get("actual_spend", 0.0)) if isinstance(project, dict) else 0.0

    def _reserved(self, project_id: str) -> float:
        project = self.ledger["projects"].get(project_id, {})
        reservations = project.get("reservations", {}) if isinstance(project, dict) else {}
        if not isinstance(reservations, dict):
            return 0.0
        return sum(
            float(record["estimated_cost"])
            for record in reservations.values()
            if isinstance(record, dict) and self._valid_amount(record.get("estimated_cost"))
        )

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
        for flag in ("enabled", "subscription_required", "zero_cost_allowed"):
            if flag in provider and not isinstance(provider[flag], bool):
                return {"allowed": False, "status": "INVALID_PROVIDER_CONFIGURATION", "provider": provider["id"]}
        if provider.get("enabled") is False:
            return {"allowed": False, "status": "PROVIDER_DISABLED", "provider": provider["id"]}

        if estimated_cost == 0:
            if provider.get("subscription_required") is True:
                return {
                    "allowed": False, "status": "COST_ESTIMATE_REQUIRED",
                    "provider": provider["id"],
                }
            if provider.get("zero_cost_allowed") is not True:
                return {
                    "allowed": False, "status": "ZERO_COST_NOT_VERIFIED",
                    "provider": provider["id"],
                }
            return {
                "allowed": True, "status": "ALLOWED_ZERO_COST", "provider": provider["id"],
                "billing_mode": "ZERO_COST", "estimated_cost": 0.0,
            }

        if free_credits_remaining > 0:
            # A numeric balance supplied by a caller is not proof of account credit.
            # Verified credit snapshots are consumed by ProviderRoutingManager.
            return {
                "allowed": False, "status": "VERIFIED_CREDIT_MANAGER_REQUIRED",
                "provider": provider["id"],
            }

        with self._transaction():
            budget = self.project_budgets.get(project_id)
            if budget is None:
                return {
                    "allowed": False, "status": "PROJECT_BUDGET_REQUIRED",
                    "provider": provider["id"], "estimated_cost": estimated_cost,
                }
            if not self._valid_amount(budget):
                return {"allowed": False, "status": "INVALID_PROJECT_BUDGET", "provider": provider["id"]}
            remaining = max(0.0, budget - self._spent(project_id) - self._reserved(project_id))
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
        if free_credits is not None and any(
            not isinstance(key, str) or not self._valid_amount(value)
            for key, value in free_credits.items()
        ):
            return {"allowed": False, "status": "INVALID_FREE_CREDIT_MAP"}
        if free_credits is not None and any(value > 0 for value in free_credits.values()):
            return {"allowed": False, "status": "VERIFIED_CREDIT_MANAGER_REQUIRED"}
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

    def reserve_usage(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        request_id: str,
    ) -> dict[str, Any]:
        """Atomically reserve budget for a request before paid execution starts."""
        if not all(isinstance(value, str) and value.strip() for value in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not self._valid_amount(estimated_cost) or estimated_cost <= 0:
            return {"status": "INVALID_COST_AMOUNT", "saved": False}
        with self._transaction():
            budget = self.project_budgets.get(project_id)
            if budget is None:
                return {"status": "PROJECT_BUDGET_REQUIRED", "saved": False}
            if not self._valid_amount(budget):
                return {"status": "INVALID_PROJECT_BUDGET", "saved": False}
            project = self.ledger["projects"].setdefault(
                project_id, {"actual_spend": 0.0, "requests": {}, "reservations": {}}
            )
            requests = project.setdefault("requests", {})
            reservations = project.setdefault("reservations", {})
            if request_id in requests:
                return {"status": "DUPLICATE_REQUEST", "saved": False, "request_id": request_id}
            existing = reservations.get(request_id)
            if existing is not None:
                if (
                    isinstance(existing, dict)
                    and existing.get("provider_id") == provider_id
                    and existing.get("estimated_cost") == float(estimated_cost)
                ):
                    return {"status": "ALREADY_RESERVED", "saved": False, "request_id": request_id}
                return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
            spent = float(project.get("actual_spend", 0.0))
            reserved = self._reserved(project_id)
            remaining = max(0.0, budget - spent - reserved)
            if estimated_cost > remaining:
                return {
                    "status": "PROJECT_BUDGET_EXCEEDED",
                    "saved": False,
                    "budget_remaining": remaining,
                }
            reservations[request_id] = {
                "provider_id": provider_id,
                "estimated_cost": float(estimated_cost),
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.ledger_path:
                self._save()
            return {
                "status": "RESERVED",
                "saved": True,
                "project_id": project_id,
                "provider_id": provider_id,
                "request_id": request_id,
                "reserved_amount": float(estimated_cost),
                "budget_remaining": round(remaining - float(estimated_cost), 6),
            }

    def finalize_usage(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        actual_cost: float,
        request_id: str,
        success: bool,
        charge_outcome: str = "unknown",
    ) -> dict[str, Any]:
        """Atomically finalize a request, including explicit no-charge confirmation.

        This method reloads and checks the idempotency key inside one ledger
        transaction so a stale in-memory snapshot cannot accept a conflicting replay.
        """
        if not all(isinstance(v, str) and v.strip() for v in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not self._valid_amount(estimated_cost) or not self._valid_amount(actual_cost):
            return {"status": "INVALID_COST_AMOUNT", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_EXECUTION_RESULT", "saved": False}
        if not isinstance(charge_outcome, str) or charge_outcome not in {"unknown", "incurred", "not_incurred"}:
            return {"status": "INVALID_CHARGE_OUTCOME", "saved": False}
        if actual_cost > 0 and charge_outcome == "not_incurred":
            return {"status": "INCONSISTENT_CHARGE_OUTCOME", "saved": False}

        with self._transaction():
            project = self.ledger["projects"].get(project_id)
            requests = project.get("requests", {}) if isinstance(project, dict) else {}
            existing = requests.get(request_id) if isinstance(requests, dict) else None
            if existing is not None:
                same_request = (
                    isinstance(existing, dict)
                    and existing.get("provider_id") == provider_id
                    and existing.get("estimated_cost") == float(estimated_cost)
                    and existing.get("actual_cost") == float(actual_cost)
                    and existing.get("execution_success", success) is success
                    and existing.get("charge_outcome", "unknown") == charge_outcome
                )
                return {
                    "status": "DUPLICATE_REQUEST" if same_request else "REQUEST_ID_CONFLICT",
                    "saved": bool(same_request),
                    "request_id": request_id,
                }

            if estimated_cost > 0 and actual_cost == 0:
                existing_project = self.ledger["projects"].get(project_id, {})
                existing_reservations = (
                    existing_project.get("reservations", {})
                    if isinstance(existing_project, dict) else {}
                )
                existing_reservation = (
                    existing_reservations.get(request_id)
                    if isinstance(existing_reservations, dict) else None
                )
                if existing_reservation is not None and (
                    not isinstance(existing_reservation, dict)
                    or existing_reservation.get("provider_id") != provider_id
                    or existing_reservation.get("estimated_cost") != float(estimated_cost)
                ):
                    return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
                if charge_outcome == "unknown":
                    return {"status": "CHARGE_OUTCOME_UNKNOWN", "saved": False, "request_id": request_id}
                if charge_outcome == "incurred":
                    return {"status": "RECONCILIATION_REQUIRED", "saved": False, "request_id": request_id}
                project = self.ledger["projects"].setdefault(
                    project_id, {"actual_spend": 0.0, "requests": {}, "reservations": {}}
                )
                reservations = project.setdefault("reservations", {})
                reservation = reservations.get(request_id)
                if reservation is not None and (
                    not isinstance(reservation, dict)
                    or reservation.get("provider_id") != provider_id
                    or reservation.get("estimated_cost") != float(estimated_cost)
                ):
                    return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
                reservations.pop(request_id, None)
                project.setdefault("requests", {})[request_id] = {
                    "provider_id": provider_id,
                    "estimated_cost": float(estimated_cost),
                    "actual_cost": 0.0,
                    "variance": round(-float(estimated_cost), 6),
                    "execution_success": success,
                    "charge_outcome": "not_incurred",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                }
                if self.ledger_path:
                    self._save()
                return {"status": "NO_CHARGE_RECORDED", "saved": True, "request_id": request_id}

            return self._reconcile_usage_unlocked(
                project_id=project_id,
                provider_id=provider_id,
                estimated_cost=estimated_cost,
                actual_cost=actual_cost,
                request_id=request_id,
                success=success,
                charge_outcome=charge_outcome,
            )

    def record_no_charge(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        request_id: str,
        success: bool = False,
    ) -> dict[str, Any]:
        """Persist a terminal, idempotent record when the provider confirms no charge."""
        if not all(isinstance(v, str) and v.strip() for v in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not self._valid_amount(estimated_cost):
            return {"status": "INVALID_COST_AMOUNT", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_EXECUTION_RESULT", "saved": False}
        with self._transaction():
            project = self.ledger["projects"].setdefault(
                project_id, {"actual_spend": 0.0, "requests": {}, "reservations": {}}
            )
            requests = project.setdefault("requests", {})
            existing = requests.get(request_id)
            if existing is not None:
                if (
                    isinstance(existing, dict)
                    and existing.get("provider_id") == provider_id
                    and existing.get("estimated_cost") == float(estimated_cost)
                    and existing.get("actual_cost") == 0.0
                    and existing.get("execution_success") is success
                    and existing.get("charge_outcome") == "not_incurred"
                ):
                    return {"status": "DUPLICATE_REQUEST", "saved": True, "request_id": request_id}
                return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
            reservations = project.setdefault("reservations", {})
            reservation = reservations.get(request_id)
            if reservation is not None and (
                not isinstance(reservation, dict)
                or reservation.get("provider_id") != provider_id
                or reservation.get("estimated_cost") != float(estimated_cost)
            ):
                return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
            reservations.pop(request_id, None)
            requests[request_id] = {
                "provider_id": provider_id,
                "estimated_cost": float(estimated_cost),
                "actual_cost": 0.0,
                "variance": round(-float(estimated_cost), 6),
                "execution_success": success,
                "charge_outcome": "not_incurred",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            if self.ledger_path:
                self._save()
            return {"status": "NO_CHARGE_RECORDED", "saved": True, "request_id": request_id}

    def release_reservation(
        self,
        *,
        project_id: str,
        request_id: str,
        provider_id: str | None = None,
        charge_outcome: str = "unknown",
    ) -> dict[str, Any]:
        """Release budget only when the caller confirms no charge was incurred.

        Ambiguous failures deliberately retain the reservation: providers may accept a
        request and charge for it even when the client times out or loses the response.
        """
        if not isinstance(project_id, str) or not project_id.strip() or not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not isinstance(charge_outcome, str) or charge_outcome not in {"not_incurred", "incurred", "unknown"}:
            return {"status": "INVALID_CHARGE_OUTCOME", "saved": False, "request_id": request_id}
        with self._transaction():
            project = self.ledger["projects"].get(project_id)
            reservations = project.get("reservations", {}) if isinstance(project, dict) else {}
            if not isinstance(reservations, dict) or request_id not in reservations:
                return {"status": "RESERVATION_NOT_FOUND", "saved": False, "request_id": request_id}
            record = reservations.get(request_id)
            if provider_id is not None and (not isinstance(record, dict) or record.get("provider_id") != provider_id):
                return {"status": "REQUEST_PROVIDER_MISMATCH", "saved": False, "request_id": request_id}
            if charge_outcome == "unknown":
                return {
                    "status": "RESERVATION_RETAINED_CHARGE_OUTCOME_UNKNOWN",
                    "saved": False,
                    "request_id": request_id,
                    "reserved_amount": record.get("estimated_cost") if isinstance(record, dict) else None,
                }
            if charge_outcome == "incurred":
                return {
                    "status": "RECONCILIATION_REQUIRED",
                    "saved": False,
                    "request_id": request_id,
                    "reserved_amount": record.get("estimated_cost") if isinstance(record, dict) else None,
                }
            reservations.pop(request_id)
            if self.ledger_path:
                self._save()
            return {"status": "RESERVATION_RELEASED", "saved": True, "request_id": request_id}

    def reconcile_usage(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        actual_cost: float,
        request_id: str,
        success: bool | None = None,
        charge_outcome: str | None = None,
    ) -> dict[str, Any]:
        """Record actual usage idempotently and flag estimate variance/budget breaches."""
        with self._transaction():
            return self._reconcile_usage_unlocked(
                project_id=project_id,
                provider_id=provider_id,
                estimated_cost=estimated_cost,
                actual_cost=actual_cost,
                request_id=request_id,
                success=success,
                charge_outcome=charge_outcome,
            )

    def _reconcile_usage_unlocked(
        self,
        *,
        project_id: str,
        provider_id: str,
        estimated_cost: float,
        actual_cost: float,
        request_id: str,
        success: bool | None = None,
        charge_outcome: str | None = None,
    ) -> dict[str, Any]:
        if not all(isinstance(v, str) and v.strip() for v in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "saved": False}
        if not self._valid_amount(estimated_cost) or not self._valid_amount(actual_cost):
            return {"status": "INVALID_COST_AMOUNT", "saved": False}
        if success is not None and not isinstance(success, bool):
            return {"status": "INVALID_EXECUTION_RESULT", "saved": False}
        if charge_outcome is not None and (
            not isinstance(charge_outcome, str)
            or charge_outcome not in {"unknown", "incurred", "not_incurred"}
        ):
            return {"status": "INVALID_CHARGE_OUTCOME", "saved": False}
        if actual_cost > 0 and charge_outcome == "not_incurred":
            return {"status": "INCONSISTENT_CHARGE_OUTCOME", "saved": False}
        if estimated_cost > 0 and actual_cost == 0 and charge_outcome != "not_incurred":
            return {
                "status": "RECONCILIATION_REQUIRED" if charge_outcome == "incurred" else "CHARGE_OUTCOME_UNKNOWN",
                "saved": False,
            }
        projects = self.ledger["projects"]
        project = projects.setdefault(
            project_id, {"actual_spend": 0.0, "requests": {}, "reservations": {}}
        )
        requests = project.setdefault("requests", {})
        if request_id in requests:
            existing = requests.get(request_id)
            same_request = (
                isinstance(existing, dict)
                and existing.get("provider_id") == provider_id
                and existing.get("estimated_cost") == float(estimated_cost)
                and existing.get("actual_cost") == float(actual_cost)
                and (success is None or existing.get("execution_success") == success)
                and (charge_outcome is None or existing.get("charge_outcome") == charge_outcome)
            )
            return {
                "status": "DUPLICATE_REQUEST" if same_request else "REQUEST_ID_CONFLICT",
                "saved": bool(same_request),
                "request_id": request_id,
            }
        reservations = project.setdefault("reservations", {})
        reservation = reservations.get(request_id)
        if reservation is not None and (
            not isinstance(reservation, dict)
            or reservation.get("provider_id") != provider_id
            or reservation.get("estimated_cost") != float(estimated_cost)
        ):
            return {"status": "REQUEST_ID_CONFLICT", "saved": False, "request_id": request_id}
        reservations.pop(request_id, None)
        record = {
            "provider_id": provider_id,
            "estimated_cost": float(estimated_cost),
            "actual_cost": float(actual_cost),
            "variance": round(float(actual_cost) - float(estimated_cost), 6),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if success is not None:
            record["execution_success"] = success
        if charge_outcome is not None:
            record["charge_outcome"] = charge_outcome
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
            atomic_replace_with_retry(temp_name, self.ledger_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
