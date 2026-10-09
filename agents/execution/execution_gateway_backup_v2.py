import sys
sys.path.insert(0, '/storage/emulated/0/AI_CREATOR_AGENT')
from projects.provider_store.unified_provider_router import UnifiedProviderRouter

class ExecutionGateway:

    def __init__(self):
        self.router = UnifiedProviderRouter()
        self.status = "READY"

    def execute(self, capability, payload=None):
        route = self.router.route(capability)

        return {
            "status": route["status"],
            "capability": capability,
            "available": route.get("available", []),
            "locked": route.get("locked", []),
            "payload_received": payload is not None
        }


if __name__ == "__main__":
    gateway = ExecutionGateway()

    print("===================================")
    print("       EXECUTION GATEWAY")
    print("===================================")

    result = gateway.execute("text_to_image", {"prompt": "test"})

    print("STATUS:", result["status"])
    print("AVAILABLE:", result["available"])
    print("READY")
