import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CATALOG = BASE / "provider_catalog.json"

class ProviderStatusManager:
    VALID_STATUSES = {
        "LOCKED",
        "CONNECTED",
        "CREDITS_UNAVAILABLE",
        "READY",
        "DISABLED"
    }

    def __init__(self):
        self.catalog = self._load()

    def _load(self):
        with open(CATALOG, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_status(self, provider_id):
        for provider in self.catalog.get("providers", []):
            if provider.get("id") == provider_id:
                return provider.get("status", "LOCKED")
        return "LOCKED"

    def is_available(self, provider_id):
        return self.get_status(provider_id) in {"CONNECTED", "READY"}

    def summary(self):
        providers = self.catalog.get("providers", [])
        result = {
            "total": len(providers),
            "CONNECTED": 0,
            "READY": 0,
            "LOCKED": 0,
            "CREDITS_UNAVAILABLE": 0,
            "DISABLED": 0
        }

        for provider in providers:
            status = provider.get("status", "LOCKED")
            if status not in self.VALID_STATUSES:
                status = "LOCKED"
            result[status] += 1

        return result

if __name__ == "__main__":
    manager = ProviderStatusManager()
    print("===================================")
    print("     PROVIDER STATUS MANAGER")
    print("===================================")
    print("SUMMARY:", manager.summary())
    print("STATUS: READY")
