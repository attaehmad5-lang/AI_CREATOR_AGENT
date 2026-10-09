from provider_store_manager import ProviderStoreManager
from provider_status_manager import ProviderStatusManager
from provider_flow import ProviderFlow

store = ProviderStoreManager()
status = ProviderStatusManager()
flow = ProviderFlow()

print("===================================")
print("       AI CREATOR PROVIDER STORE")
print("===================================")

providers = store.list_providers()

for provider in providers:
    pid = provider.get("id")
    name = provider.get("name")
    category = provider.get("category")
    current_status = provider.get("status", "LOCKED")

    print(f"[{current_status}] {name} | {category} | {pid}")

print("-----------------------------------")
print("Total Providers:", len(providers))
print("Store Status:", status.catalog.get("status", "READY"))
print("Universal Flow:", len(flow.available_providers()), "providers")
print("STATUS: READY")
