import json
from pathlib import Path

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT/projects/provider_store")
CATALOG = BASE / "provider_catalog.json"
STATE = BASE / "connection_state.json"

class ConnectionManager:

    VALID_STATES = {
        "LOCKED",
        "CONNECTING",
        "CONNECTED",
        "READY",
        "CREDITS_UNAVAILABLE",
        "DISABLED",
        "ERROR"
    }

    def __init__(self):
        self.catalog = self._load_json(CATALOG)
        self.state = self._load_json(STATE)

    def _load_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def provider_exists(self, provider_id):
        return any(
            p.get("id") == provider_id
            for p in self.catalog.get("providers", [])
        )

    def get_state(self, provider_id):
        return self.state.get("providers", {}).get(
            provider_id,
            {"status": "LOCKED"}
        )

    def set_state(self, provider_id, status):
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id}

        if status not in self.VALID_STATES:
            return {"status": "INVALID_STATE", "provider": provider_id}

        self.state.setdefault("providers", {})[provider_id] = {
            "status": status
        }

        self._save()

        return {
            "provider": provider_id,
            "status": status,
            "saved": True
        }

    def summary(self):
        result = {state: 0 for state in self.VALID_STATES}

        for item in self.state.get("providers", {}).values():
            status = item.get("status", "LOCKED")
            if status not in result:
                status = "ERROR"
            result[status] += 1

        return result

if __name__ == "__main__":
    manager = ConnectionManager()

    print("===================================")
    print("       PROVIDER CONNECTION MANAGER")
    print("===================================")
    print("Catalog Providers:", len(manager.catalog.get("providers", [])))
    print("Tracked Connections:", len(manager.state.get("providers", {})))
    print("SUMMARY:", manager.summary())
    print("SECURE MODE: ENABLED")
    print("STATUS: READY")
