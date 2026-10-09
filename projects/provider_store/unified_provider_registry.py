import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CORE = BASE / "provider_catalog.json"
ADVANCED = BASE / "advanced" / "advanced_provider_catalog.json"

class UnifiedProviderRegistry:

    def __init__(self):
        self.core = self._load(CORE)
        self.advanced = self._load(ADVANCED)

    def _load(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def all_providers(self):
        return (
            self.core.get("providers", [])
            + self.advanced.get("providers", [])
        )

    def by_category(self, category):
        return [
            p for p in self.all_providers()
            if p.get("category") == category
        ]

    def get_provider(self, provider_id):
        for provider in self.all_providers():
            if provider.get("id") == provider_id:
                return provider
        return None

    def status(self):
        providers = self.all_providers()
        return {
            "core_providers": len(self.core.get("providers", [])),
            "advanced_providers": len(self.advanced.get("providers", [])),
            "total_providers": len(providers),
            "ready": True
        }


if __name__ == "__main__":
    registry = UnifiedProviderRegistry()
    s = registry.status()

    print("===================================")
    print("     UNIFIED PROVIDER REGISTRY")
    print("===================================")
    print("Core Providers:", s["core_providers"])
    print("Advanced Providers:", s["advanced_providers"])
    print("Total Providers:", s["total_providers"])
    print("STATUS: READY")
