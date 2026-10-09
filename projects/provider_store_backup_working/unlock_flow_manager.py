import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CATALOG = BASE / "provider_catalog.json"
STATE = BASE / "connection_state.json"

class UnlockFlowManager:

    def __init__(self):
        self.catalog = self._load(CATALOG)
        self.state = self._load(STATE)

    def _load(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_provider(self, provider_id):
        for provider in self.catalog.get("providers", []):
            if provider.get("id") == provider_id:
                return provider
        return None

    def prepare(self, provider_id):
        provider = self.get_provider(provider_id)

        if provider is None:
            return {
                "status": "NOT_FOUND",
                "provider": provider_id
            }

        return {
            "status": "READY_FOR_USER_ACTION",
            "provider_id": provider.get("id"),
            "provider": provider.get("name"),
            "category": provider.get("category"),
            "current_status": provider.get("status", "LOCKED"),
            "subscription_required": provider.get(
                "subscription_required", True
            ),
            "subscription_url": provider.get(
                "subscription_url", ""
            ),
            "official_api_url": provider.get(
                "official_api_url", ""
            ),
            "connection_type": provider.get(
                "connection_type", "API"
            ),
            "next_steps": [
                "USER_SUBSCRIPTION_OR_ACCOUNT",
                "USER_CREDENTIAL_CONNECTION",
                "CONNECTION_VERIFICATION",
                "PROVIDER_ENABLE"
            ],
            "auto_purchase": False,
            "subscription_bypass": False,
            "api_key_logging": False
        }

if __name__ == "__main__":
    manager = UnlockFlowManager()

    print("===================================")
    print("        PROVIDER UNLOCK FLOW")
    print("===================================")
    print("Catalog Providers:",
          len(manager.catalog.get("providers", [])))
    print("SECURE MODE: ENABLED")
    print("AUTO PURCHASE: DISABLED")
    print("SUBSCRIPTION BYPASS: DISABLED")
    print("STATUS: READY")
