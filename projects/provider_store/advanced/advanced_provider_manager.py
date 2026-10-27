"""Compatibility facade for advanced providers in the central provider catalog."""
from __future__ import annotations

from typing import Any

from ..connection_manager import ConnectionManager
from ..unified_provider_registry import ADVANCED_CATEGORIES
from ..provider_store_manager import ProviderStoreManager
from .advanced_store_bridge import AdvancedStoreBridge


class AdvancedProviderManager:
    def __init__(
        self,
        connection_manager: ConnectionManager | None = None,
        *,
        catalog_path: str | None = None,
        state_path: str | None = None,
        provider_service: Any | None = None,
    ) -> None:
        if connection_manager is not None and (catalog_path is not None or state_path is not None):
            raise ValueError("pass either connection_manager or explicit paths, not both")
        self.store = ProviderStoreManager(
            catalog_path, state_path, connection_manager=connection_manager
        )
        # Reuse exactly the same ConnectionManager; no duplicate catalog/state.
        self.bridge = AdvancedStoreBridge(self.store.manager)
        # Execution is opt-in and always delegated to the canonical service.
        self.provider_service = provider_service

    @property
    def catalog(self) -> dict[str, Any]:
        return {"version": "1.0", "status": "READY", "providers": self.list_providers()}

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            provider for provider in self.store.list_providers()
            if provider.get("category") in ADVANCED_CATEGORIES
        ]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        provider = self.store.get_provider(provider_id)
        return provider if provider and provider.get("category") in ADVANCED_CATEGORIES else None

    def by_category(self, category: str) -> list[dict[str, Any]]:
        return [
            provider for provider in self.list_providers()
            if provider.get("category") == category
        ]

    def route_candidates(self, capability: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Return verified advanced providers eligible for a dry-run route."""
        return [
            provider for provider in self.bridge.candidates(capability, **kwargs)
            if provider.get("category") in ADVANCED_CATEGORIES
        ]

    def simulate_route(self, capability: str, **kwargs: Any) -> list[str]:
        """Return candidate IDs without making provider calls or spending credits."""
        return [provider["id"] for provider in self.route_candidates(capability, **kwargs)]

    def execute_request(self, **request: Any) -> dict[str, Any]:
        """Delegate live execution to the injected canonical ProviderService.

        Candidate selection remains a dry run. No provider call is made when the
        canonical service has not been explicitly injected.
        """
        if self.provider_service is None:
            return {"status": "PROVIDER_SERVICE_UNAVAILABLE", "executed": False}
        return self.bridge.execute_via_service(self.provider_service, **request)

    def status(self) -> dict[str, Any]:
        providers = self.list_providers()
        return {
            "version": "1.0",
            "status": "READY",
            "providers": len(providers),
            "ready": True,
        }


if __name__ == "__main__":
    manager = AdvancedProviderManager()
    print("===================================")
    print("   ADVANCED PROVIDER MANAGER")
    print("===================================")
    print("Providers:", len(manager.list_providers()))
    print("Status:", manager.status()["status"])
    print("READY")
