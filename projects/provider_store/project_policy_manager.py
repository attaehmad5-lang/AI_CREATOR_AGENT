"""Persistent project-level provider routing policy with fail-closed validation."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


PROJECT_POLICY_PATH = Path(__file__).with_name("project_provider_policies.json")


class ProviderProjectPolicyManager:
    """Persist project allow/deny, model, cost and fallback restrictions without secrets."""

    POLICY_FIELDS = {
        "allowed_providers",
        "denied_providers",
        "allowed_capabilities",
        "allowed_models",
        "max_estimated_cost",
        "automatic_fallback",
        "require_health_evidence",
        "require_verified_connection",
    }

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else PROJECT_POLICY_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load(self.path)
        self._validate_document(self.data)

    @staticmethod
    def _load(path: Path | None = None) -> dict[str, Any]:
        target = path
        if target is None:
            raise ValueError("path is required")
        try:
            with target.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "projects": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("project policy store is unreadable") from exc
        if not isinstance(data, dict):
            raise ValueError("project policy store must be an object")
        return data

    def _load_current(self) -> dict[str, Any]:
        return self._load(self.path)

    @classmethod
    def _validate_policy(cls, project_id: str, policy: Any) -> list[str]:
        errors: list[str] = []
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", project_id):
            errors.append("invalid project_id")
            return errors
        if not isinstance(policy, dict):
            return ["policy must be an object"]
        unknown = set(policy) - cls.POLICY_FIELDS
        if unknown:
            errors.append("unsupported policy fields: " + ", ".join(sorted(str(key) for key in unknown)))
        for key in ("allowed_providers", "denied_providers", "allowed_models"):
            if key not in policy:
                continue
            values = policy[key]
            if (
                not isinstance(values, list)
                or len(values) > 500
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", value)
                    for value in values
                )
                or len({value.casefold() for value in values if isinstance(value, str)}) != len(values)
            ):
                errors.append(f"{key} must be a unique list of safe identifiers")
        if "allowed_capabilities" in policy:
            values = policy["allowed_capabilities"]
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value)
                    for value in values
                )
                or len({value.casefold() for value in values if isinstance(value, str)}) != len(values)
            ):
                errors.append("allowed_capabilities must be a unique list of lowercase identifiers")
        if "max_estimated_cost" in policy and finite_number(policy["max_estimated_cost"], minimum=0) is None:
            errors.append("max_estimated_cost must be a finite non-negative number")
        for key in ("automatic_fallback", "require_health_evidence", "require_verified_connection"):
            if key in policy and not isinstance(policy[key], bool):
                errors.append(f"{key} must be a boolean")
        allowed = policy.get("allowed_providers")
        denied = policy.get("denied_providers", [])
        if isinstance(allowed, list) and isinstance(denied, list):
            overlap = {value.casefold() for value in allowed} & {value.casefold() for value in denied}
            if overlap:
                errors.append("provider cannot be both allowed and denied")
        return errors

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("projects"), dict):
            raise ValueError("invalid project policy document schema")
        for project_id, policy in data["projects"].items():
            errors = cls._validate_policy(project_id, policy)
            if errors:
                raise ValueError("invalid project policy: " + "; ".join(errors))

    def _save_unlocked(self) -> None:
        self._validate_document(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _evaluate_policy_scenario(policy: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
        reasons: list[str] = []
        provider_id = scenario["provider_id"]
        capability = scenario["capability"]
        model_id = scenario.get("model_id")
        provider_key = provider_id.casefold()

        allowed_providers = policy.get("allowed_providers")
        denied_providers = policy.get("denied_providers", [])
        if isinstance(allowed_providers, list) and provider_key not in {
            value.casefold() for value in allowed_providers
        }:
            reasons.append("PROVIDER_NOT_ALLOWLISTED")
        if provider_key in {value.casefold() for value in denied_providers}:
            reasons.append("PROVIDER_DENIED")

        allowed_capabilities = policy.get("allowed_capabilities")
        if isinstance(allowed_capabilities, list) and capability not in allowed_capabilities:
            reasons.append("CAPABILITY_NOT_ALLOWED")

        allowed_models = policy.get("allowed_models")
        if isinstance(allowed_models, list):
            if model_id is None:
                reasons.append("MODEL_ID_REQUIRED")
            elif model_id.casefold() not in {value.casefold() for value in allowed_models}:
                reasons.append("MODEL_NOT_ALLOWED")

        maximum_cost = policy.get("max_estimated_cost")
        estimated_cost = scenario.get("estimated_cost")
        if maximum_cost is not None:
            if estimated_cost is None:
                reasons.append("ESTIMATED_COST_REQUIRED")
            elif estimated_cost > maximum_cost:
                reasons.append("ESTIMATED_COST_LIMIT_EXCEEDED")

        if policy.get("require_health_evidence") is True and scenario.get("health_evidence_verified") is not True:
            reasons.append("HEALTH_EVIDENCE_REQUIRED")
        if policy.get("require_verified_connection") is True and scenario.get("connection_verified") is not True:
            reasons.append("VERIFIED_CONNECTION_REQUIRED")

        automatic_fallback_enabled = policy.get("automatic_fallback", True) is not False
        return {
            "allowed": not reasons,
            "reasons": reasons,
            "automatic_fallback_allowed": automatic_fallback_enabled,
            "fallback_requested": scenario.get("request_fallback", False),
            "fallback_would_be_blocked": (
                scenario.get("request_fallback", False) is True
                and not automatic_fallback_enabled
            ),
        }

    def simulate_policy(
        self,
        project_id: str,
        candidate_policy: dict[str, Any],
        scenarios: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Evaluate a candidate project policy without saving or changing active policy."""
        candidate_errors = self._validate_policy(project_id, candidate_policy)
        if candidate_errors:
            return {
                "status": "INVALID_CANDIDATE_POLICY",
                "project_id": project_id,
                "persisted": False,
                "errors": candidate_errors,
            }
        if not isinstance(scenarios, list) or not scenarios or len(scenarios) > 100:
            return {
                "status": "INVALID_SIMULATION_SCENARIOS",
                "project_id": project_id,
                "persisted": False,
                "errors": ["scenarios must be a non-empty list of at most 100 items"],
            }

        scenario_errors: list[dict[str, Any]] = []
        normalized_scenarios: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        allowed_fields = {
            "request_id", "provider_id", "capability", "model_id",
            "estimated_cost", "health_evidence_verified",
            "connection_verified", "request_fallback",
        }
        for index, scenario in enumerate(scenarios):
            errors: list[str] = []
            if not isinstance(scenario, dict):
                scenario_errors.append({"index": index, "errors": ["scenario must be an object"]})
                continue
            unknown = set(scenario) - allowed_fields
            if unknown:
                errors.append("unsupported scenario fields")
            request_id = scenario.get("request_id", f"scenario-{index + 1}")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", request_id):
                errors.append("invalid request_id")
            elif request_id in seen_ids:
                errors.append("duplicate request_id")
            else:
                seen_ids.add(request_id)

            provider_id = scenario.get("provider_id")
            if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", provider_id):
                errors.append("invalid provider_id")
            capability = scenario.get("capability")
            if not isinstance(capability, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", capability):
                errors.append("invalid capability")
            model_id = scenario.get("model_id")
            if model_id is not None and (
                not isinstance(model_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model_id)
            ):
                errors.append("invalid model_id")
            estimated_cost = scenario.get("estimated_cost")
            if estimated_cost is not None:
                estimated_cost = finite_number(estimated_cost, minimum=0)
                if estimated_cost is None:
                    errors.append("estimated_cost must be a finite non-negative number")
            for field in ("health_evidence_verified", "connection_verified", "request_fallback"):
                if field in scenario and not isinstance(scenario[field], bool):
                    errors.append(f"{field} must be a boolean")
            if errors:
                scenario_errors.append({"index": index, "errors": errors})
            else:
                normalized_scenarios.append({
                    "request_id": request_id,
                    "provider_id": provider_id,
                    "capability": capability,
                    "model_id": model_id,
                    "estimated_cost": estimated_cost,
                    "health_evidence_verified": scenario.get("health_evidence_verified", False),
                    "connection_verified": scenario.get("connection_verified", False),
                    "request_fallback": scenario.get("request_fallback", False),
                })
        if scenario_errors:
            return {
                "status": "INVALID_SIMULATION_SCENARIOS",
                "project_id": project_id,
                "persisted": False,
                "errors": scenario_errors,
            }

        with self._lock, interprocess_file_lock(self.lock_path):
            current_document = self._load_current()
            self._validate_document(current_document)
            current_policy = copy.deepcopy(current_document["projects"].get(project_id, {}))

        candidate = copy.deepcopy(candidate_policy)
        if "max_estimated_cost" in candidate:
            candidate["max_estimated_cost"] = finite_number(candidate["max_estimated_cost"], minimum=0)

        results: list[dict[str, Any]] = []
        changed_decisions = 0
        for scenario in normalized_scenarios:
            current_decision = self._evaluate_policy_scenario(current_policy, scenario)
            proposed_decision = self._evaluate_policy_scenario(candidate, scenario)
            changed = (
                current_decision["allowed"] != proposed_decision["allowed"]
                or current_decision["fallback_would_be_blocked"]
                != proposed_decision["fallback_would_be_blocked"]
            )
            changed_decisions += int(changed)
            results.append({
                "request_id": scenario["request_id"],
                "current": current_decision,
                "proposed": proposed_decision,
                "decision_changed": changed,
            })

        return {
            "status": "POLICY_SIMULATION_COMPLETED",
            "project_id": project_id,
            "persisted": False,
            "scenario_count": len(results),
            "changed_decisions": changed_decisions,
            "results": results,
        }

    def set_policy(self, project_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        errors = self._validate_policy(project_id, policy)
        if errors:
            return {"status": "INVALID_PROJECT_POLICY", "saved": False, "errors": errors}
        safe = copy.deepcopy(policy)
        if "max_estimated_cost" in safe:
            safe["max_estimated_cost"] = finite_number(safe["max_estimated_cost"], minimum=0)
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            self.data["projects"][project_id] = safe
            self._save_unlocked()
        return {"status": "PROJECT_POLICY_SAVED", "project_id": project_id, "saved": True}

    def get_policy(self, project_id: str) -> dict[str, Any]:
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", project_id):
            return {"status": "INVALID_PROJECT_ID", "project_id": project_id, "policy": None}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            policy = self.data["projects"].get(project_id)
            if policy is None:
                return {"status": "PROJECT_POLICY_NOT_FOUND", "project_id": project_id, "policy": None}
            return {"status": "PROJECT_POLICY_FOUND", "project_id": project_id, "policy": copy.deepcopy(policy)}

    def remove_policy(self, project_id: str) -> dict[str, Any]:
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", project_id):
            return {"status": "INVALID_PROJECT_ID", "removed": False}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            if project_id not in self.data["projects"]:
                return {"status": "PROJECT_POLICY_NOT_FOUND", "removed": False}
            del self.data["projects"][project_id]
            self._save_unlocked()
        return {"status": "PROJECT_POLICY_REMOVED", "removed": True}
