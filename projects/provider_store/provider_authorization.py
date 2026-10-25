"""Fail-closed, role-based authorization for provider operations."""
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

ROLE_PERMISSIONS = {
    "viewer": frozenset({"provider.read"}),
    "creator": frozenset({"provider.read", "provider.generate"}),
    "operator": frozenset({"provider.read", "provider.verify", "provider.generate"}),
    "billing_manager": frozenset({"provider.read", "billing.approve"}),
    "admin": frozenset({
        "provider.read", "provider.verify", "provider.generate",
        "provider.configure", "provider.enable", "provider.disable",
        "provider.credentials.rotate", "billing.approve",
    }),
}
KNOWN_OPERATIONS = frozenset().union(*ROLE_PERMISSIONS.values())
_PRINCIPAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")


class ProviderAuthorizationManager:
    """Persist explicit role assignments and deny unknown identities/operations."""

    def __init__(self, state_path: str | os.PathLike[str], *, clock: Any = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "principals": {}}
        self._reload()

    @staticmethod
    def _valid_principal(value: Any) -> bool:
        return isinstance(value, str) and _PRINCIPAL_RE.fullmatch(value) is not None

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "principals": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid authorization state JSON") from exc
        principals = loaded.get("principals") if isinstance(loaded, dict) else None
        if not isinstance(principals, dict):
            raise ValueError("invalid authorization state schema")
        for principal_id, record in principals.items():
            if (
                not self._valid_principal(principal_id)
                or not isinstance(record, dict)
                or record.get("role") not in ROLE_PERMISSIONS
                or not isinstance(record.get("enabled"), bool)
                or not isinstance(record.get("updated_at"), str)
                or ("tenant_id" in record and (
                    not isinstance(record.get("tenant_id"), str)
                    or not _PRINCIPAL_RE.fullmatch(record.get("tenant_id", ""))
                ))
            ):
                raise ValueError("invalid principal authorization record")
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
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
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

    def assign_role(
        self,
        principal_id: str,
        role: str,
        *,
        enabled: bool = True,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        if not isinstance(role, str) or role not in ROLE_PERMISSIONS:
            return {"status": "INVALID_ROLE", "saved": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_FLAG", "saved": False}
        if tenant_id is not None and not self._valid_principal(tenant_id):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        record = {
            "role": role,
            "enabled": enabled,
            "updated_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        if tenant_id is not None:
            record["tenant_id"] = tenant_id
        with self._transaction():
            self.data["principals"][principal_id] = record
            self._save()
        return {
            "status": "ROLE_ASSIGNED", "saved": True,
            "principal_id": principal_id, "role": role,
            "tenant_id": tenant_id,
        }

    def authorize(self, principal_id: str, operation: str) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"allowed": False, "status": "INVALID_PRINCIPAL_ID"}
        if not isinstance(operation, str) or operation not in KNOWN_OPERATIONS:
            return {"allowed": False, "status": "UNKNOWN_OPERATION"}
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"allowed": False, "status": "PRINCIPAL_NOT_FOUND"}
            if record.get("enabled") is not True:
                return {"allowed": False, "status": "PRINCIPAL_DISABLED"}
            role = record.get("role")
            if role not in ROLE_PERMISSIONS:
                return {"allowed": False, "status": "INVALID_ASSIGNED_ROLE"}
            allowed = operation in ROLE_PERMISSIONS[role]
            return {
                "allowed": allowed,
                "status": "AUTHORIZED" if allowed else "ACCESS_DENIED",
                "principal_id": principal_id,
                "role": role,
                "operation": operation,
            }

    def authorize_tenant(self, principal_id: str, tenant_id: str) -> dict[str, Any]:
        """Authorize access to one tenant; only admins without a tenant binding are global."""
        if not self._valid_principal(principal_id):
            return {"allowed": False, "status": "INVALID_PRINCIPAL_ID"}
        if not self._valid_principal(tenant_id):
            return {"allowed": False, "status": "INVALID_TENANT_ID"}
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"allowed": False, "status": "PRINCIPAL_NOT_FOUND"}
            if record.get("enabled") is not True:
                return {"allowed": False, "status": "PRINCIPAL_DISABLED"}
            role = record.get("role")
            if role not in ROLE_PERMISSIONS:
                return {"allowed": False, "status": "INVALID_ASSIGNED_ROLE"}
            assigned_tenant = record.get("tenant_id")
            if assigned_tenant is None:
                allowed = role == "admin"
                status = "TENANT_AUTHORIZED" if allowed else "TENANT_SCOPE_REQUIRED"
            elif assigned_tenant == tenant_id:
                allowed = True
                status = "TENANT_AUTHORIZED"
            else:
                allowed = False
                status = "TENANT_ACCESS_DENIED"
            return {
                "allowed": allowed,
                "status": status,
                "principal_id": principal_id,
                "tenant_id": tenant_id,
                "role": role,
            }

    def disable_principal(self, principal_id: str) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"status": "PRINCIPAL_NOT_FOUND", "saved": False}
            record["enabled"] = False
            record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
        return {"status": "PRINCIPAL_DISABLED", "saved": True}
