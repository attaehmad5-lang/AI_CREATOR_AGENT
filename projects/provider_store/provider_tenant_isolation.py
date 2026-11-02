"""Durable tenant isolation boundaries for provider projects and credentials.

Only opaque credential references are stored; secret material never enters this
manager. Mutating operations are low-level primitives and must be admin-gated
by their caller. Runtime authorization fails closed on missing tenant context.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}\Z")
_TENANT_FIELDS = {"principal_ids", "project_ids", "credential_refs", "updated_at"}


class ProviderTenantIsolationManager:
    """Persist tenant ownership and reject cross-tenant project/credential access."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        clock: Any = None,
        max_tenants: int = 10000,
        max_projects: int = 100000,
        max_credential_refs: int = 100000,
        max_principals_per_tenant: int = 10000,
    ) -> None:
        for value, name, maximum in (
            (max_tenants, "max_tenants", 100000),
            (max_projects, "max_projects", 1000000),
            (max_credential_refs, "max_credential_refs", 1000000),
            (max_principals_per_tenant, "max_principals_per_tenant", 100000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_tenants = max_tenants
        self.max_projects = max_projects
        self.max_credential_refs = max_credential_refs
        self.max_principals_per_tenant = max_principals_per_tenant
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": "1.0",
            "tenants": {},
            "project_owners": {},
            "credential_owners": {},
        }
        self._reload()

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and _ID_RE.fullmatch(value) is not None

    @staticmethod
    def _valid_ref(value: Any) -> bool:
        return isinstance(value, str) and _REF_RE.fullmatch(value) is not None

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {
                "version": "1.0",
                "tenants": {},
                "project_owners": {},
                "credential_owners": {},
            }
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid tenant isolation state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or set(loaded) != {"version", "tenants", "project_owners", "credential_owners"}
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("tenants"), dict)
            or not isinstance(loaded.get("project_owners"), dict)
            or not isinstance(loaded.get("credential_owners"), dict)
            or len(loaded["tenants"]) > self.max_tenants
            or len(loaded["project_owners"]) > self.max_projects
            or len(loaded["credential_owners"]) > self.max_credential_refs
        ):
            raise ValueError("invalid tenant isolation state schema or capacity")

        expected_projects: dict[str, str] = {}
        expected_credentials: dict[str, str] = {}
        for tenant_id, record in loaded["tenants"].items():
            if (
                not self._valid_id(tenant_id)
                or not isinstance(record, dict)
                or set(record) != _TENANT_FIELDS
                or not isinstance(record.get("principal_ids"), list)
                or not isinstance(record.get("project_ids"), list)
                or not isinstance(record.get("credential_refs"), list)
                or not isinstance(record.get("updated_at"), str)
            ):
                raise ValueError("invalid tenant isolation record")
            try:
                timestamp = datetime.fromisoformat(record["updated_at"])
            except ValueError as exc:
                raise ValueError("invalid tenant isolation timestamp") from exc
            if timestamp.tzinfo is None:
                raise ValueError("tenant isolation timestamp must be timezone-aware")
            principals = record["principal_ids"]
            projects = record["project_ids"]
            credentials = record["credential_refs"]
            if (
                len(principals) > self.max_principals_per_tenant
                or len(set(principals)) != len(principals)
                or any(not self._valid_id(value) for value in principals)
                or len(set(projects)) != len(projects)
                or any(not self._valid_id(value) for value in projects)
                or len(set(credentials)) != len(credentials)
                or any(not self._valid_ref(value) for value in credentials)
            ):
                raise ValueError("invalid tenant isolation membership list")
            for project_id in projects:
                if project_id in expected_projects:
                    raise ValueError("project is assigned to multiple tenants")
                expected_projects[project_id] = tenant_id
            for credential_ref in credentials:
                if credential_ref in expected_credentials:
                    raise ValueError("credential reference is assigned to multiple tenants")
                expected_credentials[credential_ref] = tenant_id
        if loaded["project_owners"] != expected_projects:
            raise ValueError("tenant project ownership index is inconsistent")
        if loaded["credential_owners"] != expected_credentials:
            raise ValueError("tenant credential ownership index is inconsistent")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=str(self.state_path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _touch(self, record: dict[str, Any]) -> None:
        record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()

    def register_tenant(self, tenant_id: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        with self._transaction():
            if tenant_id in self.data["tenants"]:
                return {"status": "TENANT_ALREADY_EXISTS", "saved": False}
            if len(self.data["tenants"]) >= self.max_tenants:
                return {"status": "TENANT_CAPACITY_REACHED", "saved": False}
            self.data["tenants"][tenant_id] = {
                "principal_ids": [],
                "project_ids": [],
                "credential_refs": [],
                "updated_at": self.clock().astimezone(timezone.utc).isoformat(),
            }
            self._save()
        return {"status": "TENANT_REGISTERED", "saved": True}

    def bind_principal(self, tenant_id: str, principal_id: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        if not self._valid_id(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if tenant is None:
                return {"status": "TENANT_NOT_FOUND", "saved": False}
            if principal_id in tenant["principal_ids"]:
                return {"status": "PRINCIPAL_ALREADY_BOUND", "saved": False}
            if len(tenant["principal_ids"]) >= self.max_principals_per_tenant:
                return {"status": "TENANT_PRINCIPAL_CAPACITY_REACHED", "saved": False}
            tenant["principal_ids"].append(principal_id)
            self._touch(tenant)
            self._save()
        return {"status": "PRINCIPAL_BOUND", "saved": True}

    def bind_project(self, tenant_id: str, project_id: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        if not self._valid_id(project_id):
            return {"status": "INVALID_PROJECT_ID", "saved": False}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if tenant is None:
                return {"status": "TENANT_NOT_FOUND", "saved": False}
            owner = self.data["project_owners"].get(project_id)
            if owner is not None:
                return {
                    "status": "PROJECT_ALREADY_BOUND" if owner == tenant_id else "PROJECT_ALREADY_OWNED",
                    "saved": False,
                }
            if len(self.data["project_owners"]) >= self.max_projects:
                return {"status": "PROJECT_CAPACITY_REACHED", "saved": False}
            tenant["project_ids"].append(project_id)
            self.data["project_owners"][project_id] = tenant_id
            self._touch(tenant)
            self._save()
        return {"status": "PROJECT_BOUND", "saved": True}

    def bind_credential_ref(self, tenant_id: str, credential_ref: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        if not self._valid_ref(credential_ref):
            return {"status": "INVALID_CREDENTIAL_REFERENCE", "saved": False}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if tenant is None:
                return {"status": "TENANT_NOT_FOUND", "saved": False}
            owner = self.data["credential_owners"].get(credential_ref)
            if owner is not None:
                return {
                    "status": "CREDENTIAL_REFERENCE_ALREADY_BOUND" if owner == tenant_id else "CREDENTIAL_REFERENCE_ALREADY_OWNED",
                    "saved": False,
                }
            if len(self.data["credential_owners"]) >= self.max_credential_refs:
                return {"status": "CREDENTIAL_REFERENCE_CAPACITY_REACHED", "saved": False}
            tenant["credential_refs"].append(credential_ref)
            self.data["credential_owners"][credential_ref] = tenant_id
            self._touch(tenant)
            self._save()
        return {"status": "CREDENTIAL_REFERENCE_BOUND", "saved": True}

    def remove_principal(self, tenant_id: str, principal_id: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id) or not self._valid_id(principal_id):
            return {"status": "INVALID_TENANT_OR_PRINCIPAL_ID", "saved": False}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if tenant is None:
                return {"status": "TENANT_NOT_FOUND", "saved": False}
            if principal_id not in tenant["principal_ids"]:
                return {"status": "PRINCIPAL_NOT_FOUND", "saved": False}
            tenant["principal_ids"].remove(principal_id)
            self._touch(tenant)
            self._save()
        return {"status": "PRINCIPAL_REMOVED", "saved": True}

    def authorize_project(
        self, tenant_id: str, project_id: str, principal_id: str | None
    ) -> dict[str, Any]:
        if not self._valid_id(tenant_id) or not self._valid_id(project_id) or not self._valid_id(principal_id):
            return {"allowed": False, "status": "INVALID_TENANT_CONTEXT"}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if (
                tenant is None
                or self.data["project_owners"].get(project_id) != tenant_id
                or principal_id not in tenant["principal_ids"]
            ):
                return {"allowed": False, "status": "TENANT_ISOLATION_DENIED"}
            return {"allowed": True, "status": "TENANT_ACCESS_AUTHORIZED"}

    def authorize_credential(
        self, tenant_id: str, credential_ref: str, principal_id: str | None
    ) -> dict[str, Any]:
        if not self._valid_id(tenant_id) or not self._valid_ref(credential_ref) or not self._valid_id(principal_id):
            return {"allowed": False, "status": "INVALID_TENANT_CONTEXT"}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if (
                tenant is None
                or self.data["credential_owners"].get(credential_ref) != tenant_id
                or principal_id not in tenant["principal_ids"]
            ):
                return {"allowed": False, "status": "TENANT_ISOLATION_DENIED"}
            return {"allowed": True, "status": "TENANT_ACCESS_AUTHORIZED"}

    def tenant_summary(self, tenant_id: str) -> dict[str, Any]:
        if not self._valid_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "summary": None}
        with self._transaction():
            tenant = self.data["tenants"].get(tenant_id)
            if tenant is None:
                return {"status": "TENANT_NOT_FOUND", "summary": None}
            return {
                "status": "TENANT_SUMMARY_AVAILABLE",
                "summary": {
                    "principal_count": len(tenant["principal_ids"]),
                    "project_count": len(tenant["project_ids"]),
                    "credential_reference_count": len(tenant["credential_refs"]),
                },
            }
