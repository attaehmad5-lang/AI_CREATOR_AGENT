"""Safe deterministic A/B experiment assignment for provider benchmarking.

This module selects cohorts only. It never invokes a provider; callers must
apply their normal authorization, quota, and final cost gates before execution.
"""
from __future__ import annotations

import copy
import hashlib
import re
import threading
from typing import Any, Callable

from .safe_numbers import finite_number

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_VARIANT_KEYS = {
    "variant_id", "provider_id", "model_id", "weight",
    "usage_mode", "approval_ref", "max_cost_per_request",
}


class ProviderABExperimentManager:
    """Validate experiment cohorts and assign requests reproducibly."""

    def __init__(
        self,
        *,
        approval_verifier: Callable[..., Any] | None = None,
        max_experiments: int = 1000,
    ) -> None:
        if approval_verifier is not None and not callable(approval_verifier):
            raise ValueError("approval_verifier must be callable")
        if (
            isinstance(max_experiments, bool)
            or not isinstance(max_experiments, int)
            or not 1 <= max_experiments <= 10000
        ):
            raise ValueError("max_experiments is outside safe limits")
        self.approval_verifier = approval_verifier
        self.max_experiments = max_experiments
        self._lock = threading.RLock()
        self._experiments: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _valid_text(value: Any, *, maximum: int = 128) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum

    def configure(
        self,
        *,
        experiment_id: str,
        modality: str,
        variants: list[dict[str, Any]],
        traffic_percent: int = 100,
    ) -> dict[str, Any]:
        """Create an immutable experiment after every variant's usage is verified.

        FREE_VERIFIED variants must have a zero spend ceiling and a verifier
        result explicitly authorizing zero cost. PAID_APPROVED variants require
        a verifier result whose approved ceiling covers the requested ceiling.
        Approval references are not retained in the experiment record.
        """
        if not isinstance(experiment_id, str) or not _ID_RE.fullmatch(experiment_id):
            return {"status": "INVALID_EXPERIMENT_ID", "configured": False}
        if not self._valid_text(modality, maximum=64):
            return {"status": "INVALID_MODALITY", "configured": False}
        if (
            isinstance(traffic_percent, bool)
            or not isinstance(traffic_percent, int)
            or not 0 <= traffic_percent <= 100
        ):
            return {"status": "INVALID_TRAFFIC_PERCENT", "configured": False}
        if not isinstance(variants, list) or not 2 <= len(variants) <= 8:
            return {"status": "INVALID_VARIANTS", "configured": False}

        prepared: list[dict[str, Any]] = []
        variant_ids: set[str] = set()
        provider_models: set[tuple[str, str]] = set()
        total_weight = 0
        for variant in variants:
            if not isinstance(variant, dict) or set(variant) != _VARIANT_KEYS:
                return {"status": "INVALID_VARIANTS", "configured": False}
            variant_id = variant["variant_id"]
            provider_id = variant["provider_id"]
            model_id = variant["model_id"]
            weight = variant["weight"]
            usage_mode = variant["usage_mode"]
            approval_ref = variant["approval_ref"]
            if not isinstance(variant_id, str) or not _ID_RE.fullmatch(variant_id):
                return {"status": "INVALID_VARIANT_ID", "configured": False}
            if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
                return {"status": "INVALID_PROVIDER_ID", "configured": False}
            if not isinstance(model_id, str) or not _ID_RE.fullmatch(model_id):
                return {"status": "INVALID_MODEL_ID", "configured": False}
            if (
                isinstance(weight, bool) or not isinstance(weight, int)
                or not 1 <= weight <= 100
            ):
                return {"status": "INVALID_VARIANT_WEIGHT", "configured": False}
            if usage_mode not in {"FREE_VERIFIED", "PAID_APPROVED"}:
                return {"status": "INVALID_USAGE_MODE", "configured": False}
            if not isinstance(approval_ref, str) or not _REF_RE.fullmatch(approval_ref):
                return {"status": "MISSING_USAGE_APPROVAL", "configured": False}
            cost_limit = finite_number(
                variant["max_cost_per_request"], minimum=0, maximum=1000000
            )
            if cost_limit is None:
                return {"status": "INVALID_COST_LIMIT", "configured": False}
            if usage_mode == "FREE_VERIFIED" and cost_limit != 0:
                return {"status": "FREE_VARIANT_HAS_NONZERO_COST", "configured": False}
            if usage_mode == "PAID_APPROVED" and cost_limit <= 0:
                return {"status": "PAID_VARIANT_REQUIRES_COST_LIMIT", "configured": False}
            if variant_id in variant_ids or (provider_id, model_id) in provider_models:
                return {"status": "DUPLICATE_VARIANT", "configured": False}

            if self.approval_verifier is None:
                return {"status": "USAGE_APPROVAL_VERIFIER_UNAVAILABLE", "configured": False}
            try:
                approval = self.approval_verifier(
                    approval_ref=approval_ref,
                    experiment_id=experiment_id,
                    provider_id=provider_id,
                    model_id=model_id,
                    usage_mode=usage_mode,
                    requested_max_cost_per_request=cost_limit,
                )
            except Exception as exc:
                return {
                    "status": "USAGE_APPROVAL_CHECK_FAILED",
                    "configured": False,
                    "error_type": type(exc).__name__,
                }
            if not isinstance(approval, dict) or approval.get("allowed") is not True:
                return {"status": "USAGE_NOT_APPROVED", "configured": False}
            approved_limit = finite_number(
                approval.get("approved_max_cost_per_request"),
                minimum=0,
                maximum=1000000,
            )
            if approved_limit is None or approved_limit < cost_limit:
                return {"status": "APPROVED_COST_LIMIT_INSUFFICIENT", "configured": False}
            if usage_mode == "FREE_VERIFIED" and approved_limit != 0:
                return {"status": "FREE_USAGE_NOT_VERIFIED", "configured": False}

            prepared.append({
                "variant_id": variant_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "weight": weight,
                "usage_mode": usage_mode,
                "approved_max_cost_per_request": approved_limit,
            })
            variant_ids.add(variant_id)
            provider_models.add((provider_id, model_id))
            total_weight += weight

        if total_weight != 100:
            return {"status": "VARIANT_WEIGHTS_MUST_TOTAL_100", "configured": False}

        experiment = {
            "experiment_id": experiment_id,
            "modality": modality,
            "traffic_percent": traffic_percent,
            "enabled": True,
            "variants": prepared,
        }
        with self._lock:
            if experiment_id in self._experiments:
                return {"status": "EXPERIMENT_ALREADY_EXISTS", "configured": False}
            if len(self._experiments) >= self.max_experiments:
                return {"status": "EXPERIMENT_CAPACITY_REACHED", "configured": False}
            self._experiments[experiment_id] = copy.deepcopy(experiment)
        return {
            "status": "EXPERIMENT_CONFIGURED",
            "configured": True,
            "experiment": copy.deepcopy(experiment),
        }

    def set_enabled(self, experiment_id: str, enabled: bool) -> dict[str, Any]:
        """Enable/disable assignment without changing cohort or spend approvals."""
        if not isinstance(experiment_id, str) or not _ID_RE.fullmatch(experiment_id):
            return {"status": "INVALID_EXPERIMENT_ID", "updated": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_FLAG", "updated": False}
        with self._lock:
            experiment = self._experiments.get(experiment_id)
            if experiment is None:
                return {"status": "EXPERIMENT_NOT_FOUND", "updated": False}
            experiment["enabled"] = enabled
        return {"status": "EXPERIMENT_UPDATED", "updated": True, "enabled": enabled}

    def assign(self, *, experiment_id: str, request_id: str) -> dict[str, Any]:
        """Return a stable cohort decision; this method never executes a provider."""
        if not isinstance(experiment_id, str) or not _ID_RE.fullmatch(experiment_id):
            return {"status": "INVALID_EXPERIMENT_ID", "assignment": None}
        if not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "assignment": None}
        with self._lock:
            experiment = copy.deepcopy(self._experiments.get(experiment_id))
        if experiment is None:
            return {"status": "EXPERIMENT_NOT_FOUND", "assignment": None}
        if experiment["enabled"] is not True:
            return {"status": "EXPERIMENT_DISABLED", "assignment": None}

        digest = hashlib.sha256(
            f"{experiment_id}:{request_id}".encode("utf-8")
        ).digest()
        bucket = int.from_bytes(digest[:8], "big") % 100
        traffic = experiment["traffic_percent"]
        if traffic == 0 or bucket >= traffic:
            return {
                "status": "CONTROL_ONLY",
                "assignment": {
                    "experiment_id": experiment_id,
                    "assignment_id": digest.hex()[:24],
                    "provider_call_permitted": False,
                    "execution_performed": False,
                    "reason": "REQUEST_OUTSIDE_EXPERIMENT_TRAFFIC",
                },
            }

        weighted_bucket = (bucket * 100) // traffic
        cumulative = 0
        selected = experiment["variants"][-1]
        for variant in experiment["variants"]:
            cumulative += variant["weight"]
            if weighted_bucket < cumulative:
                selected = variant
                break
        return {
            "status": "VARIANT_ASSIGNED",
            "assignment": {
                "experiment_id": experiment_id,
                "assignment_id": digest.hex()[:24],
                "variant_id": selected["variant_id"],
                "provider_id": selected["provider_id"],
                "model_id": selected["model_id"],
                "usage_mode": selected["usage_mode"],
                "spend_limit_per_request": selected["approved_max_cost_per_request"],
                "provider_call_permitted": True,
                "downstream_cost_gate_required": True,
                "execution_performed": False,
            },
        }

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        if not isinstance(experiment_id, str) or not _ID_RE.fullmatch(experiment_id):
            return {"status": "INVALID_EXPERIMENT_ID", "experiment": None}
        with self._lock:
            experiment = self._experiments.get(experiment_id)
            if experiment is None:
                return {"status": "EXPERIMENT_NOT_FOUND", "experiment": None}
            return {
                "status": "EXPERIMENT_AVAILABLE",
                "experiment": copy.deepcopy(experiment),
            }
