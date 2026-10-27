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

    def list_verified_enabled_providers(
        self, capability: str | None = None
    ) -> list[dict[str, Any]]:
        """Return advanced providers whose catalog is enabled and runtime connection is verified.

        This is an eligibility filter, not a guarantee that paid-usage, quota,
        credit, or output-quality policies will authorize a particular request.
        """
        if capability is not None and (
            not isinstance(capability, str) or not capability.strip()
        ):
            return []
        wanted_capability = capability.casefold().strip() if capability else None
        verified: list[dict[str, Any]] = []
        for provider in self.list_providers():
            provider_id = provider.get("id")
            catalog_status = provider.get("catalog_status", provider.get("status"))
            if (
                not isinstance(provider_id, str)
                or provider.get("enabled") is not True
                or provider.get("deprecated") is True
                or (isinstance(catalog_status, str) and catalog_status.upper() in {"LOCKED", "DISABLED", "DEPRECATED"})
            ):
                continue
            state = self.store.manager.get_state(provider_id)
            if state.get("status") != "CONNECTED" or state.get("verified") is not True:
                continue
            if wanted_capability is not None:
                raw_capabilities = provider.get("capabilities", [])
                capabilities = (
                    {item.casefold() for item in raw_capabilities if isinstance(item, str)}
                    if isinstance(raw_capabilities, list) else set()
                )
                category = provider.get("category")
                if isinstance(category, str):
                    capabilities.add(category.casefold())
                if wanted_capability not in capabilities:
                    continue
            verified.append(provider)
        return verified

    def provider_diagnostics(self, provider_id: str) -> dict[str, Any]:
        """Report catalog and live state separately without exposing credential values."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        state = self.store.manager.get_state(provider_id)
        credentials = self.store.manager.credential_status(provider_id)
        return {
            "status": "OK",
            "provider": provider_id,
            "category": provider.get("category"),
            "catalog_enabled": provider.get("enabled") is True,
            "catalog_status": provider.get("catalog_status", provider.get("status")),
            "runtime_status": state.get("status", "UNKNOWN"),
            "verified": state.get("verified") is True and state.get("status") == "CONNECTED",
            "credential_status": credentials.get("status", "UNKNOWN"),
            "credential_present": credentials.get("credential_present") is True,
        }

    def status(self) -> dict[str, Any]:
        providers = self.list_providers()
        enabled = sum(1 for provider in providers if provider.get("enabled") is True)
        verified_enabled = self.list_verified_enabled_providers()
        categories = sorted({
            provider["category"] for provider in providers
            if isinstance(provider.get("category"), str) and provider["category"]
        })
        return {
            "version": "1.0",
            "status": "READY",
            "providers": len(providers),
            "enabled": enabled,
            "connected_verified": len(verified_enabled),
            "categories": categories,
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
