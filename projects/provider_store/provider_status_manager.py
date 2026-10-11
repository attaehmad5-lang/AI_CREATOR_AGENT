"""Live provider status queries backed by the central connection manager."""
from __future__ import annotations

from typing import Any

from .connection_manager import ConnectionManager
from .provider_store_manager import ProviderStoreManager


class ProviderStatusManager:
    VALID_STATUSES = {
        "UNKNOWN", "DISCONNECTED", "CONNECTING", "CONNECTED", "ERROR", "DISABLED",
    }

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

    def get_status(self, provider_id: str) -> str:
        provider = self.manager.get_provider(provider_id)
        if provider is None:
            return "UNKNOWN"
        if provider.get("enabled") is False:
            return "DISABLED"
        state = self.manager.get_state(provider_id)
        status = state.get("status", "UNKNOWN")
        return status if status in self.VALID_STATUSES else "UNKNOWN"

    def is_available(self, provider_id: str) -> bool:
        provider = self.manager.get_provider(provider_id)
        if provider is None or provider.get("enabled") is not True:
            return False
        state = self.manager.get_state(provider_id)
        return state.get("status") == "CONNECTED" and state.get("verified") is True

    def summary(self) -> dict[str, int]:
        providers = self.store.list_providers()
        result = {"total": len(providers), **{status: 0 for status in sorted(self.VALID_STATUSES)}}
        for provider in providers:
            provider_id = provider.get("id")
            status = self.get_status(provider_id) if isinstance(provider_id, str) else "UNKNOWN"
            result[status] = result.get(status, 0) + 1
        return result


if __name__ == "__main__":
    manager = ProviderStatusManager()
    print("===================================")
    print("     PROVIDER STATUS MANAGER")
    print("===================================")
    print("SUMMARY:", manager.summary())
    print("STATUS: READY")
