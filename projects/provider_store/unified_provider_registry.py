"""Unified provider registry backed by the central validated catalog."""
from __future__ import annotations

from typing import Any

from .connection_manager import ConnectionManager
from .provider_store_manager import ProviderStoreManager


ADVANCED_CATEGORIES = {
    "script_story",
    "advanced_image_editing",
    "advanced_upscaling",
    "advanced_transcription",
    "advanced_audio",
    "advanced_video_editing",
}


class UnifiedProviderRegistry:
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
        self.connection_manager = self.store.manager

    @property
    def core(self) -> dict[str, Any]:
        providers = [
            provider for provider in self.all_providers()
            if provider.get("category") not in ADVANCED_CATEGORIES
        ]
        validation = self.connection_manager.validate_catalog_schema(self.connection_manager.catalog)
        return {
            "version": "1.0",
            "status": "READY" if validation.get("valid") is True else "ERROR",
            "catalog_valid": validation.get("valid") is True,
            "ready": validation.get("valid") is True,
            "providers": providers,
        }

    @property
    def advanced(self) -> dict[str, Any]:
        providers = [
            provider for provider in self.all_providers()
            if provider.get("category") in ADVANCED_CATEGORIES
        ]
        validation = self.connection_manager.validate_catalog_schema(self.connection_manager.catalog)
        return {
            "version": "1.0",
            "status": "READY" if validation.get("valid") is True else "ERROR",
            "catalog_valid": validation.get("valid") is True,
            "ready": validation.get("valid") is True,
            "providers": providers,
        }

    def all_providers(self) -> list[dict[str, Any]]:
        return self.store.list_providers()

    def by_category(self, category: str) -> list[dict[str, Any]]:
        if not isinstance(category, str) or not category:
            return []
        return [
            provider for provider in self.all_providers()
            if provider.get("category") == category
        ]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return self.store.get_provider(provider_id)

    def status(self) -> dict[str, Any]:
        providers = self.all_providers()
        advanced = sum(
            1 for provider in providers
            if provider.get("category") in ADVANCED_CATEGORIES
        )
        validation = self.connection_manager.validate_catalog_schema(self.connection_manager.catalog)
        catalog_valid = validation.get("valid") is True
        return {
            "core_providers": len(providers) - advanced,
            "advanced_providers": advanced,
            "total_providers": len(providers),
            "catalog_valid": catalog_valid,
            "ready": catalog_valid,
            "status": "READY" if catalog_valid else "ERROR",
        }


if __name__ == "__main__":
    registry = UnifiedProviderRegistry()
    status = registry.status()
    print("===================================")
    print("     UNIFIED PROVIDER REGISTRY")
    print("===================================")
    print("Core Providers:", status["core_providers"])
    print("Advanced Providers:", status["advanced_providers"])
    print("Total Providers:", status["total_providers"])
    print("STATUS:", status["status"])
