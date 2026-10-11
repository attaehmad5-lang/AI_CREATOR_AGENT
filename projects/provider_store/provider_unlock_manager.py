"""Provider account/subscription information with live connection state separated."""
from __future__ import annotations

from typing import Any

from .connection_manager import ConnectionManager
from .provider_store_manager import ProviderStoreManager


class ProviderUnlockManager:
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
        self.manager = self.store.manager

    @property
    def catalog(self) -> dict[str, Any]:
        return self.store.catalog

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return self.store.get_provider(provider_id)

    def connection_info(self, provider_id: str) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "verified": False}
        state = self.manager.get_state(provider_id)
        status = "DISABLED" if provider.get("enabled") is False else state.get("status", "UNKNOWN")
        return {
            "provider": provider.get("name", provider_id),
            "provider_id": provider_id,
            "category": provider.get("category", "unknown"),
            "catalog_status": provider.get("catalog_status", provider.get("status")),
            "status": status,
            "verified": state.get("verified") is True and status == "CONNECTED",
            "subscription_required": provider.get("subscription_required", True),
            "subscription_url": provider.get("subscription_url", ""),
            "official_api_url": provider.get("official_api_url", ""),
            "connection_type": provider.get("connection_type", "API"),
            "enabled": provider.get("enabled") is True,
            "credits_status": provider.get("credits_status", "UNKNOWN"),
        }


if __name__ == "__main__":
    manager = ProviderUnlockManager()
    print("===================================")
    print("     PROVIDER UNLOCK MANAGER")
    print("===================================")
    print("Providers:", len(manager.catalog.get("providers", [])))
    print("Status: READY")
