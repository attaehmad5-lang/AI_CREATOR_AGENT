"""Compatibility facade backed by the central provider connection manager.

Legacy callers keep their list/get/status API while sharing the same validated
catalog and runtime connection state as routing and provider execution.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from .connection_manager import ConnectionManager
except ImportError:  # direct script execution compatibility
    from connection_manager import ConnectionManager


class ProviderStoreManager:
    def __init__(
        self,
        catalog_path: str | Path | None = None,
        state_path: str | Path | None = None,
        *,
        connection_manager: ConnectionManager | None = None,
    ) -> None:
        if connection_manager is not None and (catalog_path is not None or state_path is not None):
            raise ValueError("pass either connection_manager or explicit paths, not both")
        self.manager = connection_manager or ConnectionManager(catalog_path, state_path)

    @property
    def catalog(self) -> dict[str, Any]:
        """Return a detached compatibility view; runtime connection state stays separate."""
        return {
            "store": "AI_CREATOR_AGENT_PROVIDER_STORE",
            "version": "1.0",
            "status": "READY",
            "subscription_policy": "USER_CONTROLLED",
            "providers": self.list_providers(),
        }

    def list_providers(self) -> list[dict[str, Any]]:
        return self.manager.list_providers()

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return self.manager.get_provider(provider_id)

    def status(self) -> dict[str, Any]:
        providers = self.list_providers()
        connected_verified = 0
        for provider in providers:
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            state = self.manager.get_state(provider_id)
            if state.get("status") == "CONNECTED" and state.get("verified") is True:
                connected_verified += 1
        return {
            "store": "AI_CREATOR_AGENT_PROVIDER_STORE",
            "version": "1.0",
            "status": "READY",
            "providers_total": len(providers),
            "connected_verified": connected_verified,
            "ready": True,
        }


if __name__ == "__main__":
    store = ProviderStoreManager()
    print("===================================")
    print("     AI PROVIDER STORE MANAGER")
    print("===================================")
    print("Providers:", len(store.list_providers()))
    print("Status:", store.status()["status"])
    print("READY")
