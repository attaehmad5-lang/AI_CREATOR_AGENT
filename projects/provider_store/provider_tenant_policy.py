"""Fail-closed tenant/project scope bindings for provider execution."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:@/-")
_MODALITIES = frozenset({"image", "audio", "video", "document", "text", "music", "voice"})


class ProviderTenantPolicyManager:
    """Bind projects to one tenant and enforce tenant/project/provider/modality scope."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_tenants: int = 10_000,
        max_projects: int = 100_000,
    ) -> None:
        if isinstance(max_tenants, bool) or not isinstance(max_tenants, int) or not 1 <= max_tenants <= 100_000:
            raise ValueError("max_tenants is outside safe limits")
        if isinstance(max_projects, bool) or not isinstance(max_projects, int) or not 1 <= max_projects <= 1_000_000:
            raise ValueError("max_projects is outside safe limits")
        self.path = Path(state_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_tenants = max_tenants
        self.max_projects = max_projects
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "tenants": {}}
        with self._transaction():
            pass

    @staticmethod
    def _valid_id(value: Any, limit: int = 128) -> bool:
        return (
            isinstance(value, str) and 1 <= len(value) <= limit
            and value.strip() == value and all(ch in _ID_CHARS for ch in value)
        )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": "1.0", "tenants": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("tenant policy state is unreadable") from None
        if not isinstance(loaded, dict) or loaded.get("version") != "1.0" or not isinstance(loaded.get("tenants"), dict):
            raise ValueError("tenant policy state schema is invalid")
        if len(loaded["tenants"]) > self.max_tenants:
            raise ValueError("tenant policy state exceeds configured limit")
        total_projects = 0
        seen_projects: set[str] = set()
        for tenant_id, tenant in loaded["tenants"].items():
            if (
                not self._valid_id(tenant_id) or not isinstance(tenant, dict)
                or tenant.get("tenant_id") != tenant_id
                or not isinstance(tenant.get("enabled"), bool)
                or not isinstance(tenant.get("projects"), dict)
            ):
                raise ValueError("tenant record is invalid")
            for project_id, project in tenant["projects"].items():
                total_projects += 1
                if (
                    not self._valid_id(project_id) or project_id in seen_projects
                    or not isinstance(project, dict) or project.get("project_id") != project_id
                    or not isinstance(project.get("enabled"), bool)
                    or not isinstance(project.get("allowed_providers"), list)
                    or not isinstance(project.get("allowed_modalities"), list)
                    or any(not self._valid_id(x) for x in project["allowed_providers"])
                    or any(not isinstance(x, str) or x not in _MODALITIES for x in project["allowed_modalities"])
                    or len(set(project["allowed_providers"])) != len(project["allowed_providers"])
                    or len(set(project["allowed_modalities"])) != len(project["allowed_modalities"])
                ):
                    raise ValueError("tenant project policy is invalid")
                seen_projects.add(project_id)
        if total_projects > self.max_projects:
            raise ValueError("tenant project state exceeds configured limit")
        return loaded

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    class _Transaction:
        def __init__(self, manager: "ProviderTenantPolicyManager") -> None:
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

    def register_tenant(self, tenant_id: str, *, enabled: bool = True) -> dict[str, Any]:
        if not self._valid_id(tenant_id) or not isinstance(enabled, bool):
            return {"status": "INVALID_TENANT", "saved": False}
        with self._transaction():
            existing = self.data["tenants"].get(tenant_id)
            if existing is not None:
                if existing.get("enabled") == enabled:
                    return {"status": "TENANT_ALREADY_EXISTS", "saved": False}
                existing["enabled"] = enabled
                return {"status": "TENANT_UPDATED", "saved": True}
            if len(self.data["tenants"]) >= self.max_tenants:
                return {"status": "TENANT_LIMIT_REACHED", "saved": False}
            self.data["tenants"][tenant_id] = {
                "tenant_id": tenant_id, "enabled": enabled, "projects": {},
            }
        return {"status": "TENANT_REGISTERED", "saved": True}

    def bind_project(
        self,
        *,
        tenant_id: str,
        project_id: str,
        allowed_providers: list[str],
        allowed_modalities: list[str],
        enabled: bool = True,
    ) -> dict[str, Any]:
        if (
            not self._valid_id(tenant_id) or not self._valid_id(project_id)
            or not isinstance(enabled, bool)
            or not isinstance(allowed_providers, list)
            or not isinstance(allowed_modalities, list)
            or any(not self._valid_id(provider_id) for provider_id in allowed_providers)
            or any(not isinstance(modality, str) or modality not in _MODALITIES for modality in allowed_modalities)
            or len(set(allowed_providers)) != len(allowed_providers)
            or len(set(allowed_modalities)) != len(allowed_modalities)
        ):
            return {"status": "INVALID_PROJECT_POLICY", "saved": False}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if not isinstance(tenant, dict):
                return {"status": "TENANT_NOT_FOUND", "saved": False}
            if tenant.get("enabled") is not True:
                return {"status": "TENANT_DISABLED", "saved": False}
            for other_tenant_id, other in self.data["tenants"].items():
                if other_tenant_id != tenant_id and project_id in other.get("projects", {}):
                    return {"status": "PROJECT_ALREADY_BOUND_TO_ANOTHER_TENANT", "saved": False}
            current = tenant["projects"].get(project_id)
            if current is None:
                total = sum(len(item.get("projects", {})) for item in self.data["tenants"].values())
                if total >= self.max_projects:
                    return {"status": "PROJECT_LIMIT_REACHED", "saved": False}
            tenant["projects"][project_id] = {
                "project_id": project_id, "enabled": enabled,
                "allowed_providers": sorted(allowed_providers),
                "allowed_modalities": sorted(allowed_modalities),
            }
        return {"status": "PROJECT_POLICY_SAVED", "saved": True}

    def authorize_scope(
        self,
        *,
        tenant_id: str,
        project_id: str,
        modality: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            not self._valid_id(tenant_id) or not self._valid_id(project_id)
            or not isinstance(modality, str) or modality not in _MODALITIES
            or (provider_id is not None and not self._valid_id(provider_id))
        ):
            return {"allowed": False, "status": "INVALID_TENANT_SCOPE"}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if not isinstance(tenant, dict):
                return {"allowed": False, "status": "TENANT_NOT_FOUND"}
            if tenant.get("enabled") is not True:
                return {"allowed": False, "status": "TENANT_DISABLED"}
            project = tenant["projects"].get(project_id)
            if not isinstance(project, dict):
                return {"allowed": False, "status": "PROJECT_NOT_BOUND_TO_TENANT"}
            if project.get("enabled") is not True:
                return {"allowed": False, "status": "PROJECT_DISABLED"}
            if modality not in project["allowed_modalities"]:
                return {"allowed": False, "status": "MODALITY_NOT_ALLOWED"}
            if provider_id is not None and provider_id not in project["allowed_providers"]:
                return {"allowed": False, "status": "PROVIDER_NOT_ALLOWED"}
            return {
                "allowed": True, "status": "TENANT_SCOPE_ALLOWED",
                "tenant_id": tenant_id, "project_id": project_id,
            }
