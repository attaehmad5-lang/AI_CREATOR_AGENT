"""Read-only provider-store CLI with honest catalog and runtime readiness diagnostics."""
from __future__ import annotations

from .advanced.advanced_provider_manager import AdvancedProviderManager
from .provider_store_manager import ProviderStoreManager


def main() -> int:
    store = ProviderStoreManager()
    providers = store.list_providers()
    advanced = AdvancedProviderManager()
    readiness = advanced.readiness_report()

    print("===================================")
    print("       AI CREATOR PROVIDER STORE")
    print("===================================")
    print("Provider catalog entries:", len(providers))
    print("Advanced catalog entries:", readiness["providers_total"])
    print("Catalog consistency:", readiness["catalog_consistency"])
    print("Category catalog:", readiness["category_catalog_status"])
    print("Advanced readiness:", readiness["status"])
    print("-----------------------------------")
    for provider in readiness["providers"]:
        if not provider["ready"]:
            blockers = ", ".join(provider["blockers"]) or "UNKNOWN"
            print(f"[BLOCKED] {provider['provider']} | {blockers}")
    print("-----------------------------------")
    print("STATUS:", "READY" if readiness["status"] == "READY" else "DEGRADED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
