"""Tenant-scoped provider policies, stored separately from project policies."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .project_policy_manager import ProviderProjectPolicyManager


TENANT_POLICY_PATH = Path(__file__).with_name("tenant_provider_policies.json")


class ProviderTenantPolicyManager:
    """Persist tenant restrictions without credentials; invalid or missing policies fail closed."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else TENANT_POLICY_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
        self._validate_document(self.data)

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "tenants": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("tenant policy store is unreadable") from exc
        if not isinstance(data, dict):
            raise ValueError("tenant policy store must be an object")
        return data

    def _load_current(self) -> dict[str, Any]:
        return self._load(self.path)

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if (
            not isinstance(data, dict) or data.get("version") != "1.0"
            or not isinstance(data.get("tenants"), dict)
        ):
            raise ValueError("invalid tenant policy document schema")
        for tenant_id, policy in data["tenants"].items():
            errors = ProviderProjectPolicyManager._validate_policy(tenant_id, policy)
            if errors:
                raise ValueError("invalid tenant policy: " + "; ".join(errors))

    def _save_unlocked(self) -> None:
        self._validate_document(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
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

    def set_policy(self, tenant_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        errors = ProviderProjectPolicyManager._validate_policy(tenant_id, policy)
        if errors:
            return {"status": "INVALID_TENANT_POLICY", "saved": False, "errors": errors}
        safe = copy.deepcopy(policy)
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            self.data["tenants"][tenant_id] = safe
            self._save_unlocked()
        return {"status": "TENANT_POLICY_SAVED", "tenant_id": tenant_id, "saved": True}

    def get_policy(self, tenant_id: str) -> dict[str, Any]:
        import re

        if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", tenant_id):
            return {"status": "INVALID_TENANT_ID", "tenant_id": None, "policy": None}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            policy = self.data["tenants"].get(tenant_id)
            if policy is None:
                return {"status": "TENANT_POLICY_NOT_FOUND", "tenant_id": tenant_id, "policy": None}
            return {"status": "TENANT_POLICY_FOUND", "tenant_id": tenant_id, "policy": copy.deepcopy(policy)}

    def remove_policy(self, tenant_id: str) -> dict[str, Any]:
        import re

        if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}", tenant_id):
            return {"status": "INVALID_TENANT_ID", "removed": False}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            if tenant_id not in self.data["tenants"]:
                return {"status": "TENANT_POLICY_NOT_FOUND", "removed": False}
            del self.data["tenants"][tenant_id]
            self._save_unlocked()
        return {"status": "TENANT_POLICY_REMOVED", "removed": True}
