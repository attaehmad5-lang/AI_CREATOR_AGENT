import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CATALOG = BASE / "provider_catalog.json"

class ProviderUnlockManager:

    def __init__(self):
        with open(CATALOG, "r", encoding="utf-8") as f:
            self.catalog = json.load(f)

    def get_provider(self, provider_id):
        for provider in self.catalog.get("providers", []):
            if provider.get("id") == provider_id:
                return provider
        return None

    def connection_info(self, provider_id):
        provider = self.get_provider(provider_id)

        if provider is None:
            return {
                "status": "NOT_FOUND",
                "provider": provider_id
            }

        return {
            "provider": provider["name"],
            "category": provider["category"],
            "status": provider.get("status", "LOCKED"),
            "subscription_required": provider.get("subscription_required", True),
            "subscription_url": provider.get("subscription_url", ""),
            "official_api_url": provider.get("official_api_url", ""),
            "connection_type": provider.get("connection_type", "API"),
            "enabled": provider.get("enabled", False),
            "credits_status": provider.get("credits_status", "UNKNOWN")
        }

if __name__ == "__main__":
    manager = ProviderUnlockManager()

    print("===================================")
    print("     PROVIDER UNLOCK MANAGER")
    print("===================================")
    print("Providers:", len(manager.catalog.get("providers", [])))
    print("Status: READY")
