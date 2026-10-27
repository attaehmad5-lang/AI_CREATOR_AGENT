"""Compatibility facade for advanced providers in the central provider catalog."""
from __future__ import annotations

import copy
import json
from pathlib import Path
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
        categories_path: str | None = None,
        advanced_catalog_path: str | None = None,
    ) -> None:
        if connection_manager is not None and (catalog_path is not None or state_path is not None):
            raise ValueError("pass either connection_manager or explicit paths, not both")
        self.store = ProviderStoreManager(
            catalog_path, state_path, connection_manager=connection_manager
        )
        self.categories_path = (
            Path(categories_path)
            if categories_path is not None
            else Path(__file__).with_name("advanced_categories.json")
        )
        self.advanced_catalog_path = (
            Path(advanced_catalog_path)
            if advanced_catalog_path is not None
            else Path(__file__).with_name("advanced_provider_catalog.json")
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

    def category_catalog(self) -> dict[str, Any]:
        """Load and validate category metadata without exposing mutable internal data."""
        try:
            data = json.loads(self.categories_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"version": "1.0", "status": "CATEGORY_CATALOG_UNAVAILABLE", "categories": {}}
        categories = data.get("categories") if isinstance(data, dict) else None
        if not isinstance(categories, dict):
            return {"version": "1.0", "status": "INVALID_CATEGORY_CATALOG", "categories": {}}
        validated: dict[str, dict[str, Any]] = {}
        for category_id, record in categories.items():
            if not isinstance(category_id, str) or not category_id.strip() or not isinstance(record, dict):
                return {"version": "1.0", "status": "INVALID_CATEGORY_CATALOG", "categories": {}}
            name = record.get("name")
            capabilities = record.get("capabilities")
            if (
                not isinstance(name, str) or not name.strip()
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) or not item.strip() for item in capabilities)
                or len(capabilities) != len(set(capabilities))
            ):
                return {"version": "1.0", "status": "INVALID_CATEGORY_CATALOG", "categories": {}}
            validated[category_id] = copy.deepcopy(record)
        return {
            "version": str(data.get("version", "1.0")),
            "status": "READY",
            "categories": validated,
        }

    def list_categories(self) -> list[dict[str, Any]]:
        """Return detached category records in stable ID order."""
        catalog = self.category_catalog()
        if catalog["status"] != "READY":
            return []
        return [
            {"id": category_id, **copy.deepcopy(record)}
            for category_id, record in sorted(catalog["categories"].items())
        ]

    def get_category(self, category: str) -> dict[str, Any] | None:
        if not isinstance(category, str) or not category.strip():
            return None
        catalog = self.category_catalog()
        record = catalog["categories"].get(category.strip()) if catalog["status"] == "READY" else None
        return {"id": category.strip(), **copy.deepcopy(record)} if record is not None else None

    def by_category(self, category: str) -> list[dict[str, Any]]:
        return [
            provider for provider in self.list_providers()
            if provider.get("category") == category
        ]

    def catalog_consistency(self) -> dict[str, Any]:
        """Compare the legacy advanced catalog with the canonical runtime catalog."""
        try:
            data = json.loads(self.advanced_catalog_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"status": "ADVANCED_CATALOG_UNAVAILABLE", "providers": 0, "differences": []}
        records = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return {"status": "INVALID_ADVANCED_CATALOG", "providers": 0, "differences": []}
        legacy: dict[str, dict[str, Any]] = {}
        differences: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"].strip():
                return {"status": "INVALID_ADVANCED_CATALOG", "providers": len(records), "differences": []}
            provider_id = record["id"]
            if provider_id in legacy:
                differences.append({"provider": provider_id, "field": "id", "issue": "duplicate_advanced_id"})
            legacy[provider_id] = record
        canonical = {
            provider["id"]: provider for provider in self.store.list_providers()
            if isinstance(provider.get("id"), str) and provider.get("category") in ADVANCED_CATEGORIES
        }
        for provider_id in sorted(set(legacy) - set(canonical)):
            differences.append({"provider": provider_id, "field": "id", "issue": "missing_from_canonical_catalog"})
        for provider_id in sorted(set(canonical) - set(legacy)):
            differences.append({"provider": provider_id, "field": "id", "issue": "missing_from_advanced_catalog"})
        fields = (
            "name", "category", "auth_type", "connection_type", "enabled",
            "subscription_required", "allowed_hosts", "token_env", "api_key_env",
            "credential_env", "official_api_url", "subscription_url",
            "zero_cost_allowed", "priority", "supported_models",
        )
        for provider_id in sorted(set(legacy) & set(canonical)):
            old, current = legacy[provider_id], canonical[provider_id]
            for field in fields:
                if old.get(field) != current.get(field):
                    differences.append({"provider": provider_id, "field": field, "issue": "metadata_mismatch"})
            old_caps = old.get("capabilities", [])
            current_caps = current.get("capabilities", [])
            if (
                not isinstance(old_caps, list) or not isinstance(current_caps, list)
                or any(not isinstance(item, str) for item in old_caps)
                or any(not isinstance(item, str) for item in current_caps)
            ):
                differences.append({"provider": provider_id, "field": "capabilities", "issue": "invalid_capability_list"})
            elif sorted(old_caps) != sorted(current_caps):
                differences.append({"provider": provider_id, "field": "capabilities", "issue": "metadata_mismatch"})
        category_data = self.category_catalog()
        if category_data["status"] != "READY":
            differences.append({"provider": "*", "field": "categories", "issue": category_data["status"]})
        else:
            for provider_id, record in legacy.items():
                category_id = record.get("category")
                category = category_data["categories"].get(category_id) if isinstance(category_id, str) else None
                capabilities = record.get("capabilities", [])
                if category is None:
                    differences.append({"provider": provider_id, "field": "category", "issue": "unknown_category"})
                elif (
                    not isinstance(capabilities, list)
                    or any(not isinstance(item, str) for item in capabilities)
                ):
                    differences.append({"provider": provider_id, "field": "capabilities", "issue": "invalid_capability_list"})
                elif not set(capabilities).issubset(set(category["capabilities"])):
                    differences.append({"provider": provider_id, "field": "capabilities", "issue": "capability_not_declared_by_category"})
        return {
            "status": "MATCH" if not differences else "MISMATCH",
            "providers": len(legacy),
            "canonical_advanced_providers": len(canonical),
            "differences": differences,
        }

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

    def readiness_report(self) -> dict[str, Any]:
        """Return actionable readiness blockers for every advanced provider, without secrets."""
        providers = self.list_providers()
        consistency = self.catalog_consistency()
        entries: list[dict[str, Any]] = []
        for provider in providers:
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            state = self.store.manager.get_state(provider_id)
            credential = self.store.manager.credential_status(provider_id)
            catalog_status = provider.get("catalog_status", provider.get("status", "UNKNOWN"))
            enabled = provider.get("enabled") is True
            runtime_verified = state.get("status") == "CONNECTED" and state.get("verified") is True
            blockers: list[str] = []
            if not enabled:
                blockers.append("PROVIDER_DISABLED")
            if isinstance(catalog_status, str) and catalog_status.upper() in {"LOCKED", "DEPRECATED", "DISABLED"}:
                blockers.append("CATALOG_" + catalog_status.upper())
            if credential.get("status") not in {"CREDENTIALS_PRESENT", "NO_CREDENTIAL_REQUIRED"}:
                blockers.append("CREDENTIAL_" + str(credential.get("status", "UNKNOWN")))
            if not runtime_verified:
                blockers.append("CONNECTION_NOT_VERIFIED")
            entries.append({
                "provider": provider_id,
                "category": provider.get("category"),
                "enabled": enabled,
                "catalog_status": catalog_status,
                "runtime_status": state.get("status", "UNKNOWN"),
                "runtime_verified": runtime_verified,
                "credential_status": credential.get("status", "UNKNOWN"),
                "ready": not blockers,
                "blockers": blockers,
            })
        ready_count = sum(1 for item in entries if item["ready"])
        return {
            "status": "READY" if entries and ready_count == len(entries) and consistency["status"] == "MATCH" else "DEGRADED",
            "providers_total": len(entries),
            "providers_ready": ready_count,
            "catalog_consistency": consistency["status"],
            "catalog_differences": consistency["differences"],
            "providers": entries,
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
