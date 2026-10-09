from unlock_flow_manager import UnlockFlowManager

class ProviderFlow:

    def __init__(self):
        self.manager = UnlockFlowManager()

    def start(self, provider_id):
        return self.manager.prepare(provider_id)

    def available_providers(self):
        return [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "category": p.get("category"),
                "status": p.get("status", "LOCKED")
            }
            for p in self.manager.catalog.get("providers", [])
        ]

if __name__ == "__main__":
    flow = ProviderFlow()

    print("===================================")
    print("       UNIVERSAL PROVIDER FLOW")
    print("===================================")
    print("Providers:", len(flow.available_providers()))
    print("Flow Type: UNIVERSAL")
    print("Subscription Bypass: DISABLED")
    print("Auto Purchase: DISABLED")
    print("STATUS: READY")
