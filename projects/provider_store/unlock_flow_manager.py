"""User-controlled provider unlock/connection preparation flow.

This module never purchases subscriptions, bypasses billing, stores credentials, or
claims a provider is connected based on a catalog label.
"""
from __future__ import annotations

from typing import Any

from .connection_manager import ConnectionManager
from .provider_store_manager import ProviderStoreManager


class UnlockFlowManager:
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

    @property
    def state(self) -> dict[str, Any]:
        return {
            "providers": {
                provider.get("id"): self.manager.get_state(provider.get("id"))
                for provider in self.store.list_providers()
                if isinstance(provider.get("id"), str)
            }
        }

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return self.store.get_provider(provider_id)

    def prepare(self, provider_id: str) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        runtime = self.manager.get_state(provider_id)
        current_status = (
            "DISABLED" if provider.get("enabled") is False
            else runtime.get("status", "UNKNOWN")
        )
        return {
            "status": "READY_FOR_USER_ACTION",
            "provider_id": provider_id,
            "provider": provider.get("name", provider_id),
            "category": provider.get("category", "unknown"),
            "catalog_status": provider.get("catalog_status", provider.get("status")),
            "current_status": current_status,
            "verified": runtime.get("verified") is True and current_status == "CONNECTED",
            "subscription_required": provider.get("subscription_required", True),
            "subscription_url": provider.get("subscription_url", ""),
            "official_api_url": provider.get("official_api_url", ""),
            "connection_type": provider.get("connection_type", "API"),
            "next_steps": [
                "USER_SUBSCRIPTION_OR_ACCOUNT",
                "USER_CREDENTIAL_CONNECTION",
                "CONNECTION_VERIFICATION",
                "PROVIDER_ENABLE",
            ],
            "auto_purchase": False,
            "subscription_bypass": False,
            "api_key_logging": False,
        }


if __name__ == "__main__":
    manager = UnlockFlowManager()
    print("===================================")
    print("        PROVIDER UNLOCK FLOW")
    print("===================================")
    print("Catalog Providers:", len(manager.catalog.get("providers", [])))
    print("SECURE MODE: ENABLED")
    print("AUTO PURCHASE: DISABLED")
    print("SUBSCRIPTION BYPASS: DISABLED")
    print("STATUS: READY")
