"""Workspace-scoped access control primitives for Project Structure / Folders.

This is a policy engine, not an identity provider. Callers must authenticate users
before asking this module to authorize an operation. Policies fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from math import isfinite
from pathlib import PurePosixPath
from threading import RLock
from time import time
from typing import Iterable


PERMISSIONS = frozenset({
    "folder.read", "folder.create", "folder.rename", "folder.move",
    "folder.delete", "file.read", "file.create", "file.update",
    "file.delete", "file.share", "workspace.admin",
})
ROLE_PERMISSIONS = {
    "viewer": frozenset({"folder.read", "file.read"}),
    "editor": frozenset({
        "folder.read", "folder.create", "folder.rename", "folder.move",
        "file.read", "file.create", "file.update",
    }),
    "manager": frozenset({
        "folder.read", "folder.create", "folder.rename", "folder.move",
        "folder.delete", "file.read", "file.create", "file.update",
        "file.delete", "file.share",
    }),
    "owner": PERMISSIONS,
}


class PolicyError(ValueError):
    """Raised when a policy or authorization request is malformed."""


def normalize_scope(scope: str) -> str:
    """Normalize a workspace-relative scope and reject unsafe or ambiguous paths."""
    if not isinstance(scope, str) or not scope.strip():
        raise PolicyError("scope must be a non-empty relative path")
    if len(scope) > 1024:
        raise PolicyError("scope exceeds the 1024-character limit")
    if any(ord(char) < 32 or ord(char) == 127 for char in scope):
        raise PolicyError("scope must not contain control characters")
    value = scope.replace("\\", "/")
    # Reject drive-qualified paths even when running on POSIX, where PurePosixPath
    # would otherwise treat a Windows path such as C:/secret as relative.
    if len(value) >= 2 and value[0].isalpha() and value[1] == ":":
        raise PolicyError("scope must not be drive-qualified")
    if value.strip() == ".":
        return "*"
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in value.split("/")):
        raise PolicyError("scope must not be absolute or contain parent traversal")
    normalized = "/".join(part for part in path.parts if part and part != ".")
    return normalized or "*"


@dataclass(frozen=True)
class PolicyRule:
    subject: str
    permission: str
    scope: str = "*"
    effect: str = "allow"
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise PolicyError("subject must be a non-empty identity")
        if not isinstance(self.permission, str) or self.permission not in PERMISSIONS:
            raise PolicyError(f"unknown permission: {self.permission}")
        if not isinstance(self.effect, str) or self.effect not in {"allow", "deny"}:
            raise PolicyError("effect must be allow or deny")
        object.__setattr__(self, "scope", normalize_scope(self.scope))
        if self.expires_at is not None:
            if (
                isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, (int, float))
                or not isfinite(self.expires_at)
                or self.expires_at <= 0
            ):
                raise PolicyError("expires_at must be a finite positive timestamp")


class WorkspacePolicy:
    """Thread-safe, in-memory role/ACL policy with deny-overrides and audit events.

    Rules are scoped to relative paths. Exact path rules also apply to descendants;
    glob wildcards may be used for intentional broader scopes. An explicit deny
    always overrides role grants or other matching allow rules.
    """

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}
        self._rules: list[PolicyRule] = []
        self._audit: list[dict] = []
        self._lock = RLock()

    def assign_role(self, subject: str, role: str) -> None:
        if not isinstance(subject, str) or not subject.strip() or subject != subject.strip():
            raise PolicyError("subject must be a non-empty identity")
        if not isinstance(role, str) or role not in ROLE_PERMISSIONS:
            raise PolicyError(f"unknown role: {role}")
        with self._lock:
            self._roles[subject] = role
            self._record(subject, "role.assign", role, "allow")

    def revoke_role(self, subject: str) -> bool:
        if not isinstance(subject, str) or not subject.strip() or subject != subject.strip():
            raise PolicyError("subject must be a non-empty identity")
        with self._lock:
            existed = self._roles.pop(subject, None) is not None
            self._record(subject, "role.revoke", "*", "allow" if existed else "deny")
            return existed

    def add_rule(self, rule: PolicyRule) -> None:
        if not isinstance(rule, PolicyRule):
            raise TypeError("rule must be a PolicyRule")
        with self._lock:
            self._rules.append(rule)
            self._record(rule.subject, "policy.add", rule.scope, rule.effect)

    def remove_rule(self, rule: PolicyRule) -> bool:
        with self._lock:
            try:
                self._rules.remove(rule)
            except ValueError:
                self._record(rule.subject, "policy.remove", rule.scope, "deny")
                return False
            self._record(rule.subject, "policy.remove", rule.scope, "allow")
            return True

    @staticmethod
    def _scope_matches(rule_scope: str, requested_scope: str) -> bool:
        if rule_scope == "*":
            return True
        if any(char in rule_scope for char in "*?["):
            return fnmatchcase(requested_scope, rule_scope)
        return requested_scope == rule_scope or requested_scope.startswith(rule_scope + "/")

    def authorize(self, subject: str, permission: str, scope: str = ".") -> bool:
        if not isinstance(subject, str) or not subject.strip() or subject != subject.strip():
            raise PolicyError("subject must be a non-empty identity")
        if not isinstance(permission, str) or permission not in PERMISSIONS:
            raise PolicyError(f"unknown permission: {permission}")
        normalized = normalize_scope(scope)
        if not isinstance(subject, str) or not subject.strip() or subject != subject.strip():
            raise PolicyError("subject must be a non-empty identity")
        with self._lock:
            now = time()
            matching = [
                rule for rule in self._rules
                if rule.subject in {subject, "*"}
                and rule.permission == permission
                and (rule.expires_at is None or rule.expires_at > now)
                and self._scope_matches(rule.scope, normalized)
            ]
            denied = any(rule.effect == "deny" for rule in matching)
            role = self._roles.get(subject)
            role_allowed = permission in ROLE_PERMISSIONS.get(role, ())
            allowed = not denied and (
                role_allowed or any(rule.effect == "allow" for rule in matching)
            )
            self._record(subject, permission, normalized, "allow" if allowed else "deny")
            return allowed

    def audit_events(self, *, limit: int = 100) -> tuple[dict, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        with self._lock:
            events = self._audit[-limit:] if limit else []
            return tuple(dict(event) for event in events)

    def _record(self, subject: str, action: str, scope: str, outcome: str) -> None:
        self._audit.append({
            "timestamp": time(), "subject": subject, "action": action,
            "scope": scope, "outcome": outcome,
        })
        # Keep memory bounded even if a caller never exports audit events.
        if len(self._audit) > 10_000:
            del self._audit[: len(self._audit) - 10_000]
