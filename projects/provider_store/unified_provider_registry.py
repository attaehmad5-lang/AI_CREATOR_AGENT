"""Unified provider registry backed by the central validated catalog."""
from __future__ import annotations

from typing import Any, Callable

from .connection_manager import ConnectionManager
from .provider_store_manager import ProviderStoreManager
from .runtime_adapter_registry import RuntimeProviderRegistry


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
        adapter_factories: dict[str, Callable[[dict[str, Any]], Any]] | None = None,
    ) -> None:
        if connection_manager is not None and (catalog_path is not None or state_path is not None):
            raise ValueError("pass either connection_manager or explicit paths, not both")
        self.store = ProviderStoreManager(
            catalog_path, state_path, connection_manager=connection_manager
        )
        self.connection_manager = self.store.manager
        self.runtime = RuntimeProviderRegistry(
            self.connection_manager, factories=adapter_factories
        )

    def load_runtime_provider(self, provider_id: str) -> dict[str, Any]:
        """Load a trusted runtime adapter through the canonical registry lifecycle."""
        return self.runtime.load(provider_id)

    def load_enabled_runtime_providers(self) -> dict[str, Any]:
        """Load every enabled provider that has an explicitly registered adapter."""
        return self.runtime.load_all_enabled()

    def unload_runtime_provider(self, provider_id: str) -> dict[str, Any]:
        """Release a process-local runtime adapter through its lifecycle manager."""
        return self.runtime.unload(provider_id)

    def runtime_status(self) -> dict[str, Any]:
        """Report catalog/adapter coverage without implying unprobed connectivity."""
        return self.runtime.status()

    @property
    def core(self) -> dict[str, Any]:
        providers = [
            provider for provider in self.all_providers()
            if provider.get("category") not in ADVANCED_CATEGORIES
        ]
        return {"version": "1.0", "status": "READY", "providers": providers}

    @property
    def advanced(self) -> dict[str, Any]:
        providers = [
            provider for provider in self.all_providers()
            if provider.get("category") in ADVANCED_CATEGORIES
        ]
        return {"version": "1.0", "status": "READY", "providers": providers}

    def all_providers(self) -> list[dict[str, Any]]:
        return self.store.list_providers()

    def by_category(self, category: str) -> list[dict[str, Any]]:
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
        return {
            "core_providers": len(providers) - advanced,
            "advanced_providers": advanced,
            "total_providers": len(providers),
            "ready": True,
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
    print("STATUS: READY")
