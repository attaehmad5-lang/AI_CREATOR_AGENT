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
        "provider.credentials.rotate", "provider.policy.simulate", "provider.access.manage",
        "billing.approve",
    }),
}
KNOWN_OPERATIONS = frozenset().union(*ROLE_PERMISSIONS.values())
PROVIDER_SCOPED_OPERATIONS = frozenset({
    "provider.read", "provider.verify", "provider.generate",
    "provider.configure", "provider.enable", "provider.disable",
    "provider.credentials.rotate",
})
_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}")
_PRINCIPAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")


class ProviderAuthorizationManager:
    """Persist role permissions and optional fail-closed provider allowlists."""

    supports_provider_scopes = True

    def __init__(
        self, state_path: str | os.PathLike[str], *, clock: Any = None,
        max_principals: int = 10_000,
    ) -> None:
        if isinstance(max_principals, bool) or not isinstance(max_principals, int) or not 1 <= max_principals <= 100_000:
            raise ValueError("max_principals must be within 1..100000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.max_principals = max_principals
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
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(principals, dict)
            or len(principals) > self.max_principals
        ):
            raise ValueError("invalid authorization state schema or principal capacity")
        for principal_id, record in principals.items():
            if (
                not self._valid_principal(principal_id)
                or not isinstance(record, dict)
                or record.get("role") not in ROLE_PERMISSIONS
                or not isinstance(record.get("enabled"), bool)
                or not isinstance(record.get("updated_at"), str)
            ):
                raise ValueError("invalid principal authorization record")
            try:
                datetime.fromisoformat(record["updated_at"])
            except ValueError as exc:
                raise ValueError("invalid principal authorization timestamp") from exc
            if "provider_scopes" in record:
                scopes = record["provider_scopes"]
                if not isinstance(scopes, dict) or len(scopes) > len(PROVIDER_SCOPED_OPERATIONS):
                    raise ValueError("invalid provider authorization scopes")
                for operation, provider_ids in scopes.items():
                    if (
                        operation not in PROVIDER_SCOPED_OPERATIONS
                        or operation not in ROLE_PERMISSIONS[record["role"]]
                        or not isinstance(provider_ids, list)
                        or len(provider_ids) > 256
                        or any(
                            not isinstance(provider_id, str)
                            or _PROVIDER_ID_RE.fullmatch(provider_id) is None
                            for provider_id in provider_ids
                        )
                        or len(set(provider_ids)) != len(provider_ids)
                    ):
                        raise ValueError("invalid provider authorization scope entry")
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

    def _active_admin_count(self) -> int:
        principals = self.data.get("principals", {})
        if not isinstance(principals, dict):
            return 0
        return sum(
            1 for record in principals.values()
            if isinstance(record, dict)
            and record.get("role") == "admin"
            and record.get("enabled") is True
        )

    def _would_remove_last_active_admin(
        self, principal_id: str, role: str, enabled: bool
    ) -> bool:
        current = self.data["principals"].get(principal_id)
        currently_admin = (
            isinstance(current, dict)
            and current.get("role") == "admin"
            and current.get("enabled") is True
        )
        remains_admin = role == "admin" and enabled is True
        return currently_admin and not remains_admin and self._active_admin_count() <= 1

    def assign_role(self, principal_id: str, role: str, *, enabled: bool = True) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        if not isinstance(role, str) or role not in ROLE_PERMISSIONS:
            return {"status": "INVALID_ROLE", "saved": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_FLAG", "saved": False}
        with self._transaction():
            if self._would_remove_last_active_admin(principal_id, role, enabled):
                return {"status": "LAST_ACTIVE_ADMIN_REQUIRED", "saved": False}
            if principal_id not in self.data["principals"] and len(self.data["principals"]) >= self.max_principals:
                return {"status": "PRINCIPAL_CAPACITY_REACHED", "saved": False}
            record = {
                "role": role,
                "enabled": enabled,
                "updated_at": self.clock().astimezone(timezone.utc).isoformat(),
            }
            previous = self.data["principals"].get(principal_id)
            if isinstance(previous, dict) and "provider_scopes" in previous:
                record["provider_scopes"] = {
                    operation: list(provider_ids)
                    for operation, provider_ids in previous["provider_scopes"].items()
                    if operation in ROLE_PERMISSIONS[role]
                }
            self.data["principals"][principal_id] = record
            self._save()
        return {"status": "ROLE_ASSIGNED", "saved": True, "principal_id": principal_id, "role": role}

    def set_provider_scopes(
        self,
        principal_id: str,
        provider_scopes: dict[str, list[str]] | None,
    ) -> dict[str, Any]:
        """Replace provider allowlists; absent operation keys deny that provider action."""
        if not self._valid_principal(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        if provider_scopes is not None and (
            not isinstance(provider_scopes, dict)
            or len(provider_scopes) > len(PROVIDER_SCOPED_OPERATIONS)
        ):
            return {"status": "INVALID_PROVIDER_SCOPES", "saved": False}
        normalized: dict[str, list[str]] | None = None
        if provider_scopes is not None:
            normalized = {}
            for operation, provider_ids in provider_scopes.items():
                if operation not in PROVIDER_SCOPED_OPERATIONS:
                    return {"status": "INVALID_SCOPED_OPERATION", "saved": False}
                if (
                    not isinstance(provider_ids, list)
                    or len(provider_ids) > 256
                    or any(
                        not isinstance(provider_id, str)
                        or _PROVIDER_ID_RE.fullmatch(provider_id) is None
                        for provider_id in provider_ids
                    )
                    or len(set(provider_ids)) != len(provider_ids)
                ):
                    return {"status": "INVALID_PROVIDER_SCOPE_LIST", "saved": False}
                normalized[operation] = list(provider_ids)
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"status": "PRINCIPAL_NOT_FOUND", "saved": False}
            role = record.get("role")
            if not isinstance(role, str) or role not in ROLE_PERMISSIONS:
                return {"status": "INVALID_ASSIGNED_ROLE", "saved": False}
            if normalized is None:
                record.pop("provider_scopes", None)
            else:
                if any(operation not in ROLE_PERMISSIONS[role] for operation in normalized):
                    return {"status": "ROLE_OPERATION_NOT_PERMITTED", "saved": False}
                record["provider_scopes"] = normalized
            record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
        return {
            "status": "PROVIDER_SCOPES_UPDATED",
            "saved": True,
            "principal_id": principal_id,
            "scoped_operations": sorted(normalized) if normalized is not None else [],
            "provider_counts": (
                {operation: len(provider_ids) for operation, provider_ids in normalized.items()}
                if normalized is not None else {}
            ),
            "global_provider_access": normalized is None,
        }

    def authorize(
        self,
        principal_id: str,
        operation: str,
        *,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"allowed": False, "status": "INVALID_PRINCIPAL_ID"}
        if not isinstance(operation, str) or operation not in KNOWN_OPERATIONS:
            return {"allowed": False, "status": "UNKNOWN_OPERATION"}
        if provider_id is not None and (
            not isinstance(provider_id, str)
            or (provider_id != "*" and _PROVIDER_ID_RE.fullmatch(provider_id) is None)
        ):
            return {"allowed": False, "status": "INVALID_PROVIDER_ID"}
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"allowed": False, "status": "PRINCIPAL_NOT_FOUND"}
            if record.get("enabled") is not True:
                return {"allowed": False, "status": "PRINCIPAL_DISABLED"}
            role = record.get("role")
            if role not in ROLE_PERMISSIONS:
                return {"allowed": False, "status": "INVALID_ASSIGNED_ROLE"}
            if operation not in ROLE_PERMISSIONS[role]:
                return {
                    "allowed": False, "status": "ACCESS_DENIED",
                    "principal_id": principal_id, "role": role, "operation": operation,
                }
            result = {
                "allowed": True, "status": "AUTHORIZED",
                "principal_id": principal_id, "role": role, "operation": operation,
            }
            scopes = record.get("provider_scopes")
            if operation in PROVIDER_SCOPED_OPERATIONS and isinstance(scopes, dict):
                allowed_ids = scopes.get(operation, [])
                if provider_id == "*":
                    if not allowed_ids:
                        return {
                            "allowed": False, "status": "PROVIDER_SCOPE_DENIED",
                            "principal_id": principal_id, "role": role, "operation": operation,
                        }
                    result["allowed_provider_ids"] = list(allowed_ids)
                    result["scope_status"] = "SCOPED"
                    return result
                if provider_id is None:
                    return {
                        "allowed": False, "status": "PROVIDER_SCOPE_REQUIRED",
                        "principal_id": principal_id, "role": role, "operation": operation,
                    }
                if provider_id not in allowed_ids:
                    return {
                        "allowed": False, "status": "PROVIDER_SCOPE_DENIED",
                        "principal_id": principal_id, "role": role, "operation": operation,
                        "provider_id": provider_id,
                    }
                result["scope_status"] = "SCOPED"
            return result

    def disable_principal(self, principal_id: str) -> dict[str, Any]:
        if not self._valid_principal(principal_id):
            return {"status": "INVALID_PRINCIPAL_ID", "saved": False}
        with self._transaction():
            record = self.data["principals"].get(principal_id)
            if not isinstance(record, dict):
                return {"status": "PRINCIPAL_NOT_FOUND", "saved": False}
            if record.get("role") == "admin" and record.get("enabled") is True and self._active_admin_count() <= 1:
                return {"status": "LAST_ACTIVE_ADMIN_REQUIRED", "saved": False}
            record["enabled"] = False
            record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
        return {"status": "PRINCIPAL_DISABLED", "saved": True}
