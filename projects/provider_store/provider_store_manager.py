import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CATALOG = BASE / "provider_catalog.json"


class ProviderStoreManager:

    def __init__(self):
        self.catalog = self._load_catalog()

    def _load_catalog(self):
        if not CATALOG.exists():
            return {
                "store": "AI_CREATOR_AGENT_PROVIDER_STORE",
                "version": "1.0",
                "status": "READY",
                "subscription_policy": "USER_CONTROLLED",
                "providers": []
            }

        with open(CATALOG, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_providers(self):
        return self.catalog.get("providers", [])

    def get_provider(self, provider_id):
        for provider in self.list_providers():
            if provider.get("id") == provider_id:
                return provider
        return None

    def status(self):
        providers = self.list_providers()

        return {
            "store": self.catalog.get("store"),
            "version": self.catalog.get("version"),
            "status": self.catalog.get("status"),
            "providers_total": len(providers),
            "ready": True
        }


if __name__ == "__main__":
    store = ProviderStoreManager()
    print("===================================")
    print("     AI PROVIDER STORE MANAGER")
    print("===================================")
    print("Providers:", len(store.list_providers()))
    print("Status:", store.status()["status"])
    print("READY")
