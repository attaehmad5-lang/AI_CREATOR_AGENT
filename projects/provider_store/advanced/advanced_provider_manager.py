"""Compatibility facade for advanced providers in the central provider catalog."""
from __future__ import annotations

from typing import Any

from ..connection_manager import ConnectionManager
from ..unified_provider_registry import ADVANCED_CATEGORIES
from ..provider_store_manager import ProviderStoreManager


class AdvancedProviderManager:
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
