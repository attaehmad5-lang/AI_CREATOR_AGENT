"""Fail-closed provider-operation allowlist policy.

This class evaluates grants only. It is not an authentication system: subject_id
must come from a trusted, already-authenticated application context.
"""
from __future__ import annotations

import re
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_OPERATION = re.compile(r"[a-z][a-z0-9_.:-]{0,119}")


class ProviderOperationPolicy:
    """Explicit subject/project/operation/provider grants with deny-by-default behavior."""

    def __init__(self, grants: list[dict[str, Any]]) -> None:
        if not isinstance(grants, list):
            raise ValueError("grants must be a list")
        self._grants: dict[tuple[str, str, str], set[str]] = {}
        for grant in grants:
            if not isinstance(grant, dict):
                raise ValueError("each grant must be an object")
            subject = grant.get("subject_id")
            project = grant.get("project_id")
            operation = grant.get("operation")
            providers = grant.get("provider_ids")
            if not isinstance(subject, str) or not _IDENTIFIER.fullmatch(subject):
                raise ValueError("grant subject_id is invalid")
            if not isinstance(project, str) or not _IDENTIFIER.fullmatch(project):
                raise ValueError("grant project_id is invalid")
            if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
                raise ValueError("grant operation is invalid")
            if (
                not isinstance(providers, list) or not providers
                or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in providers)
            ):
                raise ValueError("grant provider_ids must be a non-empty list of valid IDs")
            key = (subject, project, operation)
            self._grants.setdefault(key, set()).update(providers)

    def allowed_provider_ids(
        self,
        *,
        subject_id: str,
        operation: str,
        project_id: str,
    ) -> dict[str, Any]:
        """Return explicit provider grants; never treats missing grants as allow."""
        if not isinstance(subject_id, str) or not _IDENTIFIER.fullmatch(subject_id):
            return {"status": "ACCESS_DENIED", "provider_ids": []}
        if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
            return {"status": "ACCESS_DENIED", "provider_ids": []}
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            return {"status": "ACCESS_DENIED", "provider_ids": []}
        allowed = sorted(self._grants.get((subject_id, project_id, operation), set()))
        if not allowed:
            return {"status": "ACCESS_DENIED", "provider_ids": []}
        return {"status": "ACCESS_POLICY_FOUND", "provider_ids": allowed}
