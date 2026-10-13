"""Fail-closed role and scope authorization for provider operations.

The principal passed to this module MUST come from a trusted authentication layer.
Never build principal roles or scopes directly from client request fields.
"""
from __future__ import annotations

import re
from typing import Any


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
PROVIDER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
CAPABILITY_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,79}")

DEFAULT_ROLE_PERMISSIONS = {
    "owner": frozenset({
        "provider.route", "provider.verify", "provider.read",
        "provider.manage", "provider.credential.manage",
        "provider.paid.execute", "provider.audit.read",
    }),
    "admin": frozenset({
        "provider.route", "provider.verify", "provider.read",
        "provider.manage", "provider.credential.manage",
        "provider.paid.execute", "provider.audit.read",
    }),
    "operator": frozenset({
        "provider.route", "provider.verify", "provider.read",
        "provider.paid.execute",
    }),
    "creator": frozenset({"provider.route", "provider.read"}),
    "viewer": frozenset({"provider.read", "provider.audit.read"}),
    "service": frozenset({"provider.route", "provider.read"}),
}


class ProviderAccessControl:
    """Authorize an authenticated principal against role grants and explicit scopes."""

    def __init__(self, role_permissions: dict[str, set[str] | frozenset[str]] | None = None) -> None:
        grants = DEFAULT_ROLE_PERMISSIONS if role_permissions is None else role_permissions
        if not isinstance(grants, dict) or not grants:
            raise ValueError("role_permissions must be a non-empty mapping")
        normalized: dict[str, frozenset[str]] = {}
        for role, permissions in grants.items():
            if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", role):
                raise ValueError("invalid role identifier")
            if not isinstance(permissions, (set, frozenset)) or not permissions:
                raise ValueError("each role must have a non-empty permission set")
            if any(
                not isinstance(permission, str)
                or not re.fullmatch(r"provider\.[a-z][a-z.]{0,79}", permission)
                for permission in permissions
            ):
                raise ValueError("invalid permission identifier")
            normalized[role] = frozenset(permissions)
        self.role_permissions = normalized

    @staticmethod
    def _scope_list(principal: dict[str, Any], key: str, pattern: re.Pattern[str]) -> tuple[bool, set[str]]:
        if key not in principal:
            return True, set()
        value = principal[key]
        if not isinstance(value, (list, tuple, set, frozenset)) or len(value) > 1000:
            return False, set()
        if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in value):
            return False, set()
        return True, set(value)

    def authorize(
        self,
        principal: Any,
        *,
        action: str,
        project_id: str,
        provider_id: str | None = None,
        capability: str | None = None,
    ) -> dict[str, Any]:
        """Return a minimal safe decision; do not include identity or secret values."""
        if not isinstance(principal, dict):
            return {"allowed": False, "status": "AUTHENTICATED_PRINCIPAL_REQUIRED"}
        subject = principal.get("subject")
        if not isinstance(subject, str) or not IDENTIFIER.fullmatch(subject):
            return {"allowed": False, "status": "INVALID_PRINCIPAL"}
        if not isinstance(action, str) or not re.fullmatch(r"provider\.[a-z][a-z.]{0,79}", action):
            return {"allowed": False, "status": "INVALID_ACTION"}
        if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
            return {"allowed": False, "status": "INVALID_PROJECT_ID"}
        if provider_id is not None and (
            not isinstance(provider_id, str) or not PROVIDER_IDENTIFIER.fullmatch(provider_id)
        ):
            return {"allowed": False, "status": "INVALID_PROVIDER_ID"}
        if capability is not None and (
            not isinstance(capability, str) or not CAPABILITY_IDENTIFIER.fullmatch(capability)
        ):
            return {"allowed": False, "status": "INVALID_CAPABILITY"}

        roles = principal.get("roles")
        if not isinstance(roles, (list, tuple, set, frozenset)) or not roles or len(roles) > 32:
            return {"allowed": False, "status": "INVALID_PRINCIPAL_ROLES"}
        if any(not isinstance(role, str) or role not in self.role_permissions for role in roles):
            return {"allowed": False, "status": "UNKNOWN_PRINCIPAL_ROLE"}
        permissions = set().union(*(self.role_permissions[role] for role in roles))
        if action not in permissions:
            return {"allowed": False, "status": "PERMISSION_DENIED"}

        valid, projects = self._scope_list(principal, "projects", IDENTIFIER)
        if not valid:
            return {"allowed": False, "status": "INVALID_PROJECT_SCOPE"}
        if projects and project_id not in projects:
            return {"allowed": False, "status": "PROJECT_SCOPE_DENIED"}

        valid, providers = self._scope_list(principal, "providers", PROVIDER_IDENTIFIER)
        if not valid:
            return {"allowed": False, "status": "INVALID_PROVIDER_SCOPE"}
        if providers and provider_id is not None and provider_id not in providers:
            return {"allowed": False, "status": "PROVIDER_SCOPE_DENIED"}

        valid, capabilities = self._scope_list(principal, "capabilities", CAPABILITY_IDENTIFIER)
        if not valid:
            return {"allowed": False, "status": "INVALID_CAPABILITY_SCOPE"}
        if capabilities and capability is not None and capability not in capabilities:
            return {"allowed": False, "status": "CAPABILITY_SCOPE_DENIED"}

        return {"allowed": True, "status": "AUTHORIZED", "action": action}
