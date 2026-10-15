"""Deny-by-default, tenant- and project-scoped provider access control.

This module makes authorization decisions only. Callers must invoke it at every
service/API boundary; it does not authenticate the principal or replace secret
storage. Credential/token values are deliberately never exposed by this API.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class ProviderAccessController:
    """Small explicit RBAC policy for provider-management operations."""

    ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
        "reader": frozenset({"provider:read", "health:read", "usage:read"}),
        "operator": frozenset({
            "provider:read", "health:read", "usage:read",
            "provider:connect", "provider:route", "provider:diagnose",
        }),
        "admin": frozenset({
            "provider:read", "health:read", "usage:read",
            "provider:connect", "provider:route", "provider:diagnose",
            "provider:configure", "provider:enable", "provider:disable",
            "provider:register", "policy:simulate", "audit:read",
        }),
        "security_admin": frozenset({
            "provider:read", "health:read", "usage:read",
            "provider:diagnose", "credential:rotate", "policy:manage",
            "audit:read",
        }),
    }

    # These operations are never granted by an ordinary role, including admin.
    NEVER_ALLOWED = frozenset({
        "credential:read", "secret:read", "token:read", "provider:purchase",
    })
    KNOWN_ACTIONS = frozenset().union(*ROLE_PERMISSIONS.values(), NEVER_ALLOWED)

    def __init__(self, *, role_permissions: dict[str, set[str] | frozenset[str]] | None = None) -> None:
        source = self.ROLE_PERMISSIONS if role_permissions is None else role_permissions
        if not isinstance(source, dict):
            raise ValueError("role_permissions must be a mapping")
        normalized: dict[str, frozenset[str]] = {}
        for role, actions in source.items():
            if not isinstance(role, str) or not role or len(role) > 64:
                raise ValueError("invalid role name")
            if not isinstance(actions, (set, frozenset, list, tuple)):
                raise ValueError("role permissions must be a collection of actions")
            if any(
                not isinstance(action, str) or action not in self.KNOWN_ACTIONS
                for action in actions
            ):
                raise ValueError("role contains an unknown action")
            if any(action in self.NEVER_ALLOWED for action in actions):
                raise ValueError("sensitive secret/purchase actions cannot be granted")
            normalized[role] = frozenset(actions)
        self._role_permissions = normalized

    @staticmethod
    def _identifier(value: Any, *, maximum: int = 128) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= maximum
            and value == value.strip()
            and not any(ord(char) < 32 for char in value)
        )

    def authorize(
        self,
        principal: dict[str, Any],
        action: str,
        resource: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a safe decision; missing, malformed or cross-tenant context denies."""
        safe_principal = principal if isinstance(principal, dict) else {}
        safe_resource = resource if isinstance(resource, dict) else {}
        subject_id = safe_principal.get("subject_id")
        tenant_id = safe_principal.get("tenant_id")
        roles = safe_principal.get("roles")
        project_ids = safe_principal.get("project_ids", [])
        provider_id = safe_resource.get("provider_id")
        resource_tenant = safe_resource.get("tenant_id")
        project_id = safe_resource.get("project_id")

        reason = "DENY_BY_DEFAULT"
        allowed = False

        if not self._identifier(subject_id) or not self._identifier(tenant_id):
            reason = "INVALID_PRINCIPAL"
        elif not isinstance(roles, list) or not roles or any(
            not self._identifier(role, maximum=64) for role in roles
        ):
            reason = "INVALID_ROLES"
        elif not isinstance(project_ids, list) or any(
            not self._identifier(item) for item in project_ids
        ):
            reason = "INVALID_PROJECT_SCOPE"
        elif not self._identifier(provider_id) or not self._identifier(resource_tenant):
            reason = "INVALID_RESOURCE"
        elif project_id is not None and not self._identifier(project_id):
            reason = "INVALID_RESOURCE"
        elif not isinstance(action, str) or action not in self.KNOWN_ACTIONS:
            reason = "UNKNOWN_ACTION"
        elif action in self.NEVER_ALLOWED:
            reason = "SENSITIVE_ACTION_FORBIDDEN"
        elif resource_tenant != tenant_id:
            reason = "TENANT_SCOPE_MISMATCH"
        elif project_id is not None and project_id not in project_ids:
            reason = "PROJECT_SCOPE_MISMATCH"
        else:
            granted: set[str] = set()
            unknown_roles = []
            for role in roles:
                permissions = self._role_permissions.get(role)
                if permissions is None:
                    unknown_roles.append(role)
                else:
                    granted.update(permissions)
            if unknown_roles:
                # A malformed or unrecognized role must not be ignored as if valid.
                reason = "UNKNOWN_ROLE"
            elif action in granted:
                allowed = True
                reason = "PERMITTED"
            else:
                reason = "ROLE_NOT_PERMITTED"

        # Deliberately omit tokens, credential fields, arbitrary principal/resource
        # data, and policy internals from the decision/audit-safe event.
        return {
            "allowed": allowed,
            "status": "ALLOWED" if allowed else "DENIED",
            "reason": reason,
            "audit_event": {
                "subject_id": subject_id if self._identifier(subject_id) else None,
                "tenant_id": tenant_id if self._identifier(tenant_id) else None,
                "action": action if isinstance(action, str) and len(action) <= 128 else None,
                "provider_id": provider_id if self._identifier(provider_id) else None,
                "resource_tenant_id": resource_tenant if self._identifier(resource_tenant) else None,
                "project_id": project_id if self._identifier(project_id) else None,
                "allowed": allowed,
                "reason": reason,
            },
        }

    def permissions_for(self, role: str) -> tuple[str, ...]:
        """Return sorted permission names, never credential material."""
        if not isinstance(role, str) or role not in self._role_permissions:
            return ()
        return tuple(sorted(self._role_permissions[role]))
