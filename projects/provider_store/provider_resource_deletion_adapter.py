"""Explicit provider-side resource deletion adapter registry.

The application supplies provider-specific, documented deletion handlers. No
provider is considered deletable merely because it supports generation.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\Z")
_IDEMPOTENCY_KEY = re.compile(r"retention-delete:[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\Z")


class ProviderResourceDeletionAdapter:
    """Dispatch opaque resource deletion to explicitly registered provider handlers.

    Handlers receive only a remote resource reference, tenant identifier, and
    stable idempotency key. They must implement the provider's documented
    deletion API and return True or {"deleted": True} only after confirmation.
    """

    def __init__(self, handlers: Mapping[str, Callable[..., Any]]) -> None:
        if not isinstance(handlers, Mapping) or len(handlers) > 256:
            raise ValueError("provider deletion handlers must be a bounded mapping")
        clean: dict[str, Callable[..., Any]] = {}
        for provider_id, handler in handlers.items():
            if not isinstance(provider_id, str) or _ID.fullmatch(provider_id) is None:
                raise ValueError("invalid provider deletion handler ID")
            if not callable(handler):
                raise ValueError("provider deletion handler must be callable")
            clean[provider_id] = handler
        self._handlers = clean

    def delete_resource(
        self, *, provider_id: str, resource_ref: str,
        tenant_id: str, idempotency_key: str,
    ) -> dict[str, Any]:
        """Delete one opaque resource, returning a sanitized confirmation only."""
        if not all(_ID.fullmatch(value) is not None for value in (
            provider_id, resource_ref, tenant_id,
        ) if isinstance(value, str)) or not all(
            isinstance(value, str) and _ID.fullmatch(value) is not None
            for value in (provider_id, resource_ref, tenant_id)
        ):
            return {"deleted": False, "status": "INVALID_PROVIDER_RESOURCE_CONTEXT"}
        if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
            return {"deleted": False, "status": "INVALID_DELETION_IDEMPOTENCY_KEY"}
        handler = self._handlers.get(provider_id)
        if handler is None:
            return {"deleted": False, "status": "PROVIDER_DELETION_UNSUPPORTED"}
        try:
            result = handler(
                resource_ref=resource_ref,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            return {
                "deleted": False,
                "status": "PROVIDER_DELETION_FAILED",
                "error_type": type(exc).__name__,
            }
        confirmed = result is True or (
            isinstance(result, dict) and result.get("deleted") is True
        )
        return {
            "deleted": confirmed,
            "status": "PROVIDER_RESOURCE_DELETED" if confirmed else "PROVIDER_DELETION_UNCONFIRMED",
        }
