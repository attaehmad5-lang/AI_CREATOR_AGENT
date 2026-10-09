import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store/advanced")
CATALOG = BASE / "advanced_provider_catalog.json"

class AdvancedProviderManager:

    def __init__(self):
        with open(CATALOG, "r", encoding="utf-8") as f:
            self.catalog = json.load(f)

    def list_providers(self):
        return self.catalog.get("providers", [])

    def get_provider(self, provider_id):
        for provider in self.list_providers():
            if provider.get("id") == provider_id:
                return provider
        return None

    def by_category(self, category):
        return [
            p for p in self.list_providers()
            if p.get("category") == category
        ]

    def status(self):
        return {
            "version": self.catalog.get("version"),
            "status": self.catalog.get("status"),
            "providers": len(self.list_providers()),
            "ready": True
        }

if __name__ == "__main__":
    manager = AdvancedProviderManager()

    print("===================================")
    print("   ADVANCED PROVIDER MANAGER")
    print("===================================")
    print("Providers:", len(manager.list_providers()))
    print("Status:", manager.catalog.get("status", "READY"))
    print("READY")
