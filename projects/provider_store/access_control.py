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

    def __init__(
        self,
        role_permissions: dict[str, set[str] | frozenset[str]] | None = None,
        *,
        audit_logger: Any | None = None,
    ) -> None:
        if audit_logger is not None and not callable(getattr(audit_logger, "record", None)):
            raise ValueError("audit_logger must support record")
        self.audit_logger = audit_logger
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

    def _decision(
        self,
        allowed: bool,
        status: str,
        *,
        action: Any = None,
        project_id: Any = None,
        provider_id: Any = None,
    ) -> dict[str, Any]:
        result = {"allowed": allowed, "status": status}
        if allowed and isinstance(action, str) and re.fullmatch(r"provider\.[a-z][a-z.]{0,79}", action):
            result["action"] = action
        if self.audit_logger is not None:
            event = {
                "event_type": "authorization",
                "decision": "allowed" if allowed else "denied",
                "action": action if isinstance(action, str) and re.fullmatch(r"provider\.[a-z][a-z.]{0,79}", action) else "provider.authorize",
                "status": status if isinstance(status, str) and IDENTIFIER.fullmatch(status) else "AUTHORIZATION_DENIED",
            }
            if isinstance(project_id, str) and IDENTIFIER.fullmatch(project_id):
                event["project_id"] = project_id
            if isinstance(provider_id, str) and PROVIDER_IDENTIFIER.fullmatch(provider_id):
                event["provider_id"] = provider_id
            try:
                audit = self.audit_logger.record(event)
            except Exception:
                audit = {"saved": False}
            if not isinstance(audit, dict) or audit.get("saved") is not True:
                if allowed:
                    return {"allowed": False, "status": "AUDIT_LOG_WRITE_FAILED"}
        return result

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

    def provider_scope(self, principal: Any) -> dict[str, Any]:
        """Return provider allowlist for pre-routing filtering; None means unrestricted."""
        if not isinstance(principal, dict):
            return {"status": "AUTHENTICATED_PRINCIPAL_REQUIRED", "valid": False}
        subject = principal.get("subject")
        roles = principal.get("roles")
        if not isinstance(subject, str) or not IDENTIFIER.fullmatch(subject):
            return {"status": "INVALID_PRINCIPAL", "valid": False}
        if not isinstance(roles, (list, tuple, set, frozenset)) or not roles or len(roles) > 32:
            return {"status": "INVALID_PRINCIPAL_ROLES", "valid": False}
        if any(not isinstance(role, str) or role not in self.role_permissions for role in roles):
            return {"status": "UNKNOWN_PRINCIPAL_ROLE", "valid": False}
        valid, providers = self._scope_list(principal, "providers", PROVIDER_IDENTIFIER)
        if not valid:
            return {"status": "INVALID_PROVIDER_SCOPE", "valid": False}
        return {
            "status": "PROVIDER_SCOPE_VALID",
            "valid": True,
            "restricted": "providers" in principal,
            "providers": sorted(providers),
        }

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
            return self._decision(False, "AUTHENTICATED_PRINCIPAL_REQUIRED", action=action, project_id=project_id, provider_id=provider_id)
        subject = principal.get("subject")
        if not isinstance(subject, str) or not IDENTIFIER.fullmatch(subject):
            return self._decision(False, "INVALID_PRINCIPAL", action=action, project_id=project_id, provider_id=provider_id)
        if not isinstance(action, str) or not re.fullmatch(r"provider\.[a-z][a-z.]{0,79}", action):
            return self._decision(False, "INVALID_ACTION", action=action, project_id=project_id, provider_id=provider_id)
        if not isinstance(project_id, str) or not IDENTIFIER.fullmatch(project_id):
            return self._decision(False, "INVALID_PROJECT_ID", action=action, project_id=project_id, provider_id=provider_id)
        if provider_id is not None and (
            not isinstance(provider_id, str) or not PROVIDER_IDENTIFIER.fullmatch(provider_id)
        ):
            return self._decision(False, "INVALID_PROVIDER_ID", action=action, project_id=project_id, provider_id=provider_id)
        if capability is not None and (
            not isinstance(capability, str) or not CAPABILITY_IDENTIFIER.fullmatch(capability)
        ):
            return self._decision(False, "INVALID_CAPABILITY", action=action, project_id=project_id, provider_id=provider_id)

        roles = principal.get("roles")
        if not isinstance(roles, (list, tuple, set, frozenset)) or not roles or len(roles) > 32:
            return self._decision(False, "INVALID_PRINCIPAL_ROLES", action=action, project_id=project_id, provider_id=provider_id)
        if any(not isinstance(role, str) or role not in self.role_permissions for role in roles):
            return self._decision(False, "UNKNOWN_PRINCIPAL_ROLE", action=action, project_id=project_id, provider_id=provider_id)
        permissions = set().union(*(self.role_permissions[role] for role in roles))
        if action not in permissions:
            return self._decision(False, "PERMISSION_DENIED", action=action, project_id=project_id, provider_id=provider_id)

        if "projects" not in principal:
            return self._decision(False, "PROJECT_SCOPE_REQUIRED", action=action, project_id=project_id, provider_id=provider_id)
        valid, projects = self._scope_list(principal, "projects", IDENTIFIER)
        if not valid:
            return self._decision(False, "INVALID_PROJECT_SCOPE", action=action, project_id=project_id, provider_id=provider_id)
        if project_id not in projects:
            return self._decision(False, "PROJECT_SCOPE_DENIED", action=action, project_id=project_id, provider_id=provider_id)

        valid, providers = self._scope_list(principal, "providers", PROVIDER_IDENTIFIER)
        if not valid:
            return self._decision(False, "INVALID_PROVIDER_SCOPE", action=action, project_id=project_id, provider_id=provider_id)
        if providers and provider_id is not None and provider_id not in providers:
            return self._decision(False, "PROVIDER_SCOPE_DENIED", action=action, project_id=project_id, provider_id=provider_id)

        valid, capabilities = self._scope_list(principal, "capabilities", CAPABILITY_IDENTIFIER)
        if not valid:
            return self._decision(False, "INVALID_CAPABILITY_SCOPE", action=action, project_id=project_id, provider_id=provider_id)
        if capabilities and capability is not None and capability not in capabilities:
            return self._decision(False, "CAPABILITY_SCOPE_DENIED", action=action, project_id=project_id, provider_id=provider_id)

        return self._decision(True, "AUTHORIZED", action=action, project_id=project_id, provider_id=provider_id)
