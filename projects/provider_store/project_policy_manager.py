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
        "required_region",
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
        if target.is_symlink():
            raise ValueError("project policy store must not be a symlink")
        try:
            if target.stat().st_size > 4_000_000:
                raise ValueError("project policy store exceeds the size limit")

            def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate project policy JSON key")
                    result[key] = value
                return result

            with target.open("r", encoding="utf-8") as stream:
                data = json.load(
                    stream,
                    object_pairs_hook=reject_duplicate_keys,
                    parse_constant=lambda _value: (_ for _ in ()).throw(
                        ValueError("non-standard JSON constant")
                    ),
                )
        except FileNotFoundError:
            return {"version": "1.0", "projects": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as exc:
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
        if "required_region" in policy and (
            not isinstance(policy["required_region"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,31}", policy["required_region"])
        ):
            errors.append("required_region must be a safe region identifier")
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
