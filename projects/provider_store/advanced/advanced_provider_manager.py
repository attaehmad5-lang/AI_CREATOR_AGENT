"""Compatibility facade for advanced providers in the central provider catalog."""
from __future__ import annotations

from typing import Any

from ..connection_manager import ConnectionManager
from ..unified_provider_registry import ADVANCED_CATEGORIES
from ..provider_store_manager import ProviderStoreManager


class AdvancedProviderManager:
    """Read-only advanced-provider view with evidence-based runtime status.

    Provider definitions are owned by ConnectionManager's validated central
    catalog. This facade intentionally does not invent independent provider
    state or treat catalog labels as proof of a live connection.
    """

    def __init__(
        self,
        connection_manager: ConnectionManager | None = None,
        *,
        catalog_path: str | None = None,
        state_path: str | None = None,
    ) -> None:
        if connection_manager is not None and (catalog_path is not None or state_path is not None):
            raise ValueError("pass either connection_manager or explicit paths, not both")
        self.store = ProviderStoreManager(
            catalog_path, state_path, connection_manager=connection_manager
        )

    @property
    def catalog(self) -> dict[str, Any]:
        return {
            "version": "1.0",
            "status": "READY",
            "providers": self.list_providers(),
        }

    def list_providers(self) -> list[dict[str, Any]]:
        """Return detached provider views belonging to an advanced category."""
        return [
            provider for provider in self.store.list_providers()
            if provider.get("category") in ADVANCED_CATEGORIES
        ]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        """Return a provider only when it is in an advanced category."""
        provider = self.store.get_provider(provider_id)
        return provider if provider and provider.get("category") in ADVANCED_CATEGORIES else None

    def by_category(self, category: str) -> list[dict[str, Any]]:
        """List advanced providers in one exact, declared category."""
        if category not in ADVANCED_CATEGORIES:
            return []
        return [
            provider for provider in self.list_providers()
            if provider.get("category") == category
        ]

    def status(self) -> dict[str, Any]:
        """Separate catalog readiness from enabled and verified runtime state."""
        providers = self.list_providers()
        enabled = sum(provider.get("enabled") is True for provider in providers)
        disabled = sum(provider.get("enabled") is False for provider in providers)
        connected_verified = 0
        for provider in providers:
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            state = self.store.manager.get_state(provider_id)
            if state.get("status") == "CONNECTED" and state.get("verified") is True:
                connected_verified += 1

        validation = self.store.manager.validate_catalog_schema(self.store.manager.catalog)
        return {
            "version": "1.0",
            # READY means the validated catalog is accessible, not that every
            # provider is enabled or connected to its external service.
            "status": "READY" if validation.get("valid") is True else "ERROR",
            "providers": len(providers),
            "providers_enabled": enabled,
            "providers_disabled": disabled,
            "connected_verified": connected_verified,
            "catalog_valid": validation.get("valid") is True,
            "ready": validation.get("valid") is True,
        }


if __name__ == "__main__":
    manager = AdvancedProviderManager()
    status = manager.status()
    print("===================================")
    print("   ADVANCED PROVIDER MANAGER")
    print("===================================")
    print("Providers:", status["providers"])
    print("Enabled:", status["providers_enabled"])
    print("Disabled:", status["providers_disabled"])
    print("Verified connections:", status["connected_verified"])
    print("Catalog:", status["status"])
