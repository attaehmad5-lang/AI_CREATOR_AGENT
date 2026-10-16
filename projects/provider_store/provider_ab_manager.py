"""Controlled A/B benchmark assignment with fail-closed paid-spend gates.

This module chooses and records experiment arms; it does not invoke providers.
Paid arms require an explicit approval reference and an injected, idempotent
budget-reservation callback before an assignment is returned.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


class ProviderABExperimentManager:
    """Persistent, deterministic A/B assignments with bounded and audited spend."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_experiments: int = 500,
        max_assignments_per_experiment: int = 100_000,
    ) -> None:
        if isinstance(max_experiments, bool) or not isinstance(max_experiments, int) or not 1 <= max_experiments <= 10_000:
            raise ValueError("max_experiments must be between 1 and 10000")
        if isinstance(max_assignments_per_experiment, bool) or not isinstance(max_assignments_per_experiment, int) or not 1 <= max_assignments_per_experiment <= 1_000_000:
            raise ValueError("max_assignments_per_experiment must be between 1 and 1000000")
        self.path = Path(state_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_experiments = max_experiments
        self.max_assignments = max_assignments_per_experiment
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "experiments": {}}
        with self._transaction():
            pass

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": "1.0", "experiments": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("experiment state is unreadable") from None
        if not isinstance(raw, dict) or raw.get("version") != "1.0" or not isinstance(raw.get("experiments"), dict):
            raise ValueError("experiment state schema is invalid")
        if len(raw["experiments"]) > self.max_experiments:
            raise ValueError("experiment state exceeds configured limit")
        for exp_id, exp in raw["experiments"].items():
            if not self._valid_id(exp_id) or not isinstance(exp, dict):
                raise ValueError("experiment state contains invalid experiment")
            if exp.get("status") not in {"ACTIVE", "PAUSED", "STOPPED"}:
                raise ValueError("experiment state contains invalid status")
            if not isinstance(exp.get("requests"), dict) or not isinstance(exp.get("outcomes"), dict):
                raise ValueError("experiment request/outcome state is invalid")
            if len(exp["requests"]) > self.max_assignments:
                raise ValueError("experiment assignment state exceeds configured limit")
            for request_id, assignment in exp["requests"].items():
                if not self._valid_id(request_id) or not isinstance(assignment, dict):
                    raise ValueError("experiment assignment is invalid")
                if not isinstance(assignment.get("arm_id"), str) or assignment["arm_id"] not in {
                    arm.get("arm_id") for arm in exp.get("arms", []) if isinstance(arm, dict)
                }:
                    raise ValueError("experiment assignment references an unknown arm")
                if finite_number(assignment.get("estimated_cost"), minimum=0) is None:
                    raise ValueError("experiment assignment cost is invalid")
            for request_id, outcome in exp["outcomes"].items():
                if request_id not in exp["requests"] or not isinstance(outcome, dict):
                    raise ValueError("experiment outcome has no assignment")
                if not isinstance(outcome.get("success"), bool):
                    raise ValueError("experiment outcome success flag is invalid")
                if finite_number(outcome.get("latency_ms"), minimum=0) is None:
                    raise ValueError("experiment outcome latency is invalid")
                cost = finite_number(outcome.get("actual_cost"), minimum=0)
                quality = outcome.get("quality_score")
                if cost is None or (quality is not None and (finite_number(quality, minimum=0) is None or quality > 1)):
                    raise ValueError("experiment outcome metrics are invalid")
        return raw

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    class _Transaction:
        def __init__(self, manager: "ProviderABExperimentManager") -> None:
            self.manager = manager
        def __enter__(self):
            self.manager._lock.acquire()
            self.lock = interprocess_file_lock(self.manager.lock_path)
            self.lock.__enter__()
            try:
                self.manager.data = self.manager._load()
            except Exception:
                self.lock.__exit__(None, None, None)
                self.manager._lock.release()
                raise
            return self.manager
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.manager._save()
            finally:
                self.lock.__exit__(exc_type, exc, tb)
                self.manager._lock.release()
            return False

    def _transaction(self):
        return self._Transaction(self)

    @staticmethod
    def _valid_id(value: Any, limit: int = 160) -> bool:
        return isinstance(value, str) and 1 <= len(value.strip()) <= limit and value == value.strip() and not any(ord(ch) < 32 for ch in value)

    @staticmethod
    def _valid_arm_id(value: Any) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 64 and all(ch.isalnum() or ch in "_-" for ch in value)

    def create_experiment(
        self,
        *,
        experiment_id: str,
        project_id: str,
        modality: str,
        arms: list[dict[str, Any]],
        paid_usage_approved: bool = False,
        approval_reference: str | None = None,
        experiment_budget_cap: float | None = None,
        project_budget_cap: float | None = None,
        max_assignments: int = 1000,
    ) -> dict[str, Any]:
        if not self._valid_id(experiment_id, 128) or not self._valid_id(project_id, 128) or not self._valid_id(modality, 64):
            return {"status": "INVALID_EXPERIMENT_METADATA", "saved": False}
        if not isinstance(paid_usage_approved, bool):
            return {"status": "INVALID_PAID_APPROVAL_FLAG", "saved": False}
        if isinstance(max_assignments, bool) or not isinstance(max_assignments, int) or not 1 <= max_assignments <= self.max_assignments:
            return {"status": "INVALID_ASSIGNMENT_LIMIT", "saved": False}
        if not isinstance(arms, list) or not 2 <= len(arms) <= 8:
            return {"status": "INVALID_EXPERIMENT_ARMS", "saved": False}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for arm in arms:
            if not isinstance(arm, dict):
                return {"status": "INVALID_EXPERIMENT_ARM", "saved": False}
            arm_id, provider_id, model_id = arm.get("arm_id"), arm.get("provider_id"), arm.get("model_id")
            cost = finite_number(arm.get("estimated_cost", 0), minimum=0)
            weight = finite_number(arm.get("weight", 1), minimum=0)
            if (
                not self._valid_arm_id(arm_id) or arm_id in seen
                or not self._valid_id(provider_id, 128) or not self._valid_id(model_id, 256)
                or cost is None or weight is None or weight <= 0
            ):
                return {"status": "INVALID_EXPERIMENT_ARM", "saved": False}
            seen.add(arm_id)
            normalized.append({"arm_id": arm_id, "provider_id": provider_id, "model_id": model_id, "estimated_cost": cost, "weight": weight})
        has_paid_arm = any(arm["estimated_cost"] > 0 for arm in normalized)
        budget_cap = None
        if has_paid_arm:
            exp_cap = finite_number(experiment_budget_cap, minimum=0)
            project_cap = finite_number(project_budget_cap, minimum=0)
            if (
                not paid_usage_approved or not self._valid_id(approval_reference, 200)
                or exp_cap is None or exp_cap <= 0 or project_cap is None or project_cap <= 0
                or exp_cap > project_cap
            ):
                return {"status": "PAID_AB_REQUIRES_APPROVAL_AND_BUDGET_CAPS", "saved": False}
            budget_cap = exp_cap
        elif paid_usage_approved and not self._valid_id(approval_reference, 200):
            return {"status": "APPROVAL_REFERENCE_REQUIRED", "saved": False}
        with self._transaction():
            if experiment_id in self.data["experiments"]:
                return {"status": "EXPERIMENT_ALREADY_EXISTS", "saved": False}
            if len(self.data["experiments"]) >= self.max_experiments:
                return {"status": "EXPERIMENT_LIMIT_REACHED", "saved": False}
            self.data["experiments"][experiment_id] = {
                "experiment_id": experiment_id, "project_id": project_id, "modality": modality,
                "arms": normalized, "status": "ACTIVE", "paid_usage_approved": paid_usage_approved,
                "approval_reference": approval_reference if paid_usage_approved else None,
                "experiment_budget_cap": budget_cap, "project_budget_cap": (
                    finite_number(project_budget_cap, minimum=0) if has_paid_arm else None
                ),
                "reserved_cost": 0.0, "max_assignments": max_assignments,
                "requests": {}, "outcomes": {},
            }
        return {"status": "EXPERIMENT_CREATED", "saved": True, "experiment_id": experiment_id}

    def assign(
        self,
        *,
        experiment_id: str,
        project_id: str,
        request_id: str,
        reserve_paid_usage: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        if not self._valid_id(experiment_id, 128) or not self._valid_id(project_id, 128) or not self._valid_id(request_id, 200):
            return {"status": "INVALID_ASSIGNMENT_METADATA", "assigned": False}
        with self._transaction():
            exp = self.data["experiments"].get(experiment_id)
            if not isinstance(exp, dict):
                return {"status": "EXPERIMENT_NOT_FOUND", "assigned": False}
            if exp["project_id"] != project_id:
                return {"status": "PROJECT_SCOPE_MISMATCH", "assigned": False}
            if exp["status"] != "ACTIVE":
                return {"status": "EXPERIMENT_NOT_ACTIVE", "assigned": False}
            existing = exp["requests"].get(request_id)
            if existing:
                return {"status": "ALREADY_ASSIGNED", "assigned": True, **dict(existing)}
            if len(exp["requests"]) >= exp["max_assignments"]:
                return {"status": "ASSIGNMENT_LIMIT_REACHED", "assigned": False}
            digest = hashlib.sha256(f"{experiment_id}\0{project_id}\0{request_id}".encode()).digest()
            bucket = int.from_bytes(digest[:8], "big") / float(2**64)
            total_weight = sum(arm["weight"] for arm in exp["arms"])
            threshold = bucket * total_weight
            chosen = exp["arms"][-1]
            cumulative = 0.0
            for arm in exp["arms"]:
                cumulative += arm["weight"]
                if threshold < cumulative:
                    chosen = arm
                    break
            cost = chosen["estimated_cost"]
            if cost > 0:
                if not exp["paid_usage_approved"] or not exp.get("approval_reference") or not callable(reserve_paid_usage):
                    return {"status": "PAID_ARM_BLOCKED_NO_RESERVATION", "assigned": False}
                if exp["reserved_cost"] + cost > exp["experiment_budget_cap"]:
                    return {"status": "EXPERIMENT_BUDGET_EXHAUSTED", "assigned": False}
                if exp["reserved_cost"] + cost > exp["project_budget_cap"]:
                    return {"status": "PROJECT_BUDGET_CAP_REACHED", "assigned": False}
                try:
                    reservation = reserve_paid_usage(
                        project_id=project_id, request_id=f"ab:{experiment_id}:{request_id}",
                        estimated_cost=cost, approval_reference=exp["approval_reference"],
                    )
                except Exception:
                    return {"status": "PAID_BUDGET_RESERVATION_FAILED", "assigned": False}
                if not isinstance(reservation, dict) or reservation.get("saved") is not True:
                    return {"status": "PAID_BUDGET_RESERVATION_FAILED", "assigned": False}
                exp["reserved_cost"] = round(exp["reserved_cost"] + cost, 8)
            assignment = {
                "arm_id": chosen["arm_id"], "provider_id": chosen["provider_id"],
                "model_id": chosen["model_id"], "estimated_cost": cost,
            }
            exp["requests"][request_id] = assignment
        return {"status": "ASSIGNED", "assigned": True, **assignment}

    def record_outcome(
        self,
        *,
        experiment_id: str,
        project_id: str,
        request_id: str,
        success: bool,
        latency_ms: float,
        actual_cost: float,
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        cost = finite_number(actual_cost, minimum=0)
        quality = None if quality_score is None else finite_number(quality_score, minimum=0)
        if latency is None or cost is None or (quality_score is not None and (quality is None or quality > 1)):
            return {"status": "INVALID_OUTCOME_METRICS", "saved": False}
        with self._transaction():
            exp = self.data["experiments"].get(experiment_id)
            if not isinstance(exp, dict):
                return {"status": "EXPERIMENT_NOT_FOUND", "saved": False}
            if exp["project_id"] != project_id:
                return {"status": "PROJECT_SCOPE_MISMATCH", "saved": False}
            if request_id not in exp["requests"]:
                return {"status": "ASSIGNMENT_NOT_FOUND", "saved": False}
            existing = exp["outcomes"].get(request_id)
            outcome = {"success": success, "latency_ms": latency, "actual_cost": cost, "quality_score": quality}
            if existing is not None:
                if existing == outcome:
                    return {"status": "ALREADY_RECORDED", "saved": True}
                return {"status": "OUTCOME_CONFLICT", "saved": False}
            exp["outcomes"][request_id] = outcome
        return {"status": "OUTCOME_RECORDED", "saved": True}

    def summary(self, *, experiment_id: str, project_id: str) -> dict[str, Any]:
        with self._transaction():
            exp = self.data["experiments"].get(experiment_id)
            if not isinstance(exp, dict):
                return {"status": "EXPERIMENT_NOT_FOUND", "results": []}
            if exp["project_id"] != project_id:
                return {"status": "PROJECT_SCOPE_MISMATCH", "results": []}
            results = []
            for arm in exp["arms"]:
                assignments = [r for r in exp["requests"].values() if r["arm_id"] == arm["arm_id"]]
                outcomes = [exp["outcomes"][rid] for rid, r in exp["requests"].items() if r["arm_id"] == arm["arm_id"] and rid in exp["outcomes"]]
                successes = [o for o in outcomes if o["success"]]
                results.append({
                    "arm_id": arm["arm_id"], "provider_id": arm["provider_id"], "model_id": arm["model_id"],
                    "assignment_count": len(assignments), "outcome_count": len(outcomes),
                    "success_rate": len(successes) / len(outcomes) if outcomes else None,
                    "avg_latency_ms": sum(o["latency_ms"] for o in outcomes) / len(outcomes) if outcomes else None,
                    "avg_actual_cost": sum(o["actual_cost"] for o in outcomes) / len(outcomes) if outcomes else None,
                    "avg_quality_score": (
                        sum(o["quality_score"] for o in outcomes if o["quality_score"] is not None)
                        / len([o for o in outcomes if o["quality_score"] is not None])
                        if any(o["quality_score"] is not None for o in outcomes) else None
                    ),
                })
        return {"status": "AB_SUMMARY_AVAILABLE", "results": results}
