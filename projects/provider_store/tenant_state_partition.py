"""Tenant-specific state path derivation for storage partitioning.

This helper prevents path collisions and traversal; it does not magically isolate managers
that continue using shared paths. Every credential, quota, job, usage and asset store must
be explicitly constructed with its tenant-specific path before isolation can be claimed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class TenantStatePartition:
    RESOURCES = frozenset({
        "credentials", "quotas", "jobs", "usage", "assets",
        "provider_connections", "provider_health", "provider_config",
    })
    _TENANT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
    _FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.json\Z")
    _SECRET_MARKERS = ("token", "secret", "password", "api_key", "authorization", "credential", "private_key")

    def __init__(self, root: str | Path, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not self._TENANT_ID.fullmatch(tenant_id) or tenant_id in {".", ".."}:
            raise ValueError("tenant_id is invalid")
        self.root = Path(root).resolve()
        self.tenant_id = tenant_id
        self.tenant_root = self.root / tenant_id

    def path_for(self, resource: str, filename: str) -> Path:
        if not isinstance(resource, str) or resource not in self.RESOURCES:
            raise ValueError("resource is not an approved tenant store")
        if (
            not isinstance(filename, str) or not self._FILE_NAME.fullmatch(filename)
            or any(marker in filename.casefold() for marker in self._SECRET_MARKERS)
        ):
            raise ValueError("filename must be a safe, non-secret JSON basename")
        if self.tenant_root.is_symlink():
            raise ValueError("tenant storage root must not be a symlink")
        resource_dir = self.tenant_root / resource
        if resource_dir.is_symlink():
            raise ValueError("tenant resource directory must not be a symlink")
        result = resource_dir / filename
        resolved_parent = result.parent.resolve()
        try:
            resolved_parent.relative_to(self.root)
        except ValueError:
            raise ValueError("tenant path escapes the configured root") from None
        return result

    def initialize(self) -> dict[str, Any]:
        try:
            if self.tenant_root.is_symlink():
                return {"status": "TENANT_STORAGE_ROOT_SYMLINK", "initialized": False}
            self.tenant_root.mkdir(parents=True, exist_ok=True)
            for resource in sorted(self.RESOURCES):
                directory = self.tenant_root / resource
                if directory.is_symlink():
                    return {"status": "TENANT_RESOURCE_SYMLINK", "initialized": False}
                directory.mkdir(parents=True, exist_ok=True)
                try:
                    directory.resolve().relative_to(self.root)
                except ValueError:
                    return {"status": "TENANT_PATH_ESCAPE", "initialized": False}
        except OSError:
            return {"status": "TENANT_STORAGE_INITIALIZATION_FAILED", "initialized": False}
        return {"status": "TENANT_STORAGE_INITIALIZED", "initialized": True, "resource_count": len(self.RESOURCES)}
