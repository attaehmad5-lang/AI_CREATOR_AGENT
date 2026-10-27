from pathlib import Path
import sys

# Resolve the repository root from this file instead of hard-coding one device path.
_REPOSITORY_ROOT = str(Path(__file__).resolve().parents[2])
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)
from projects.provider_store.unified_provider_router import UnifiedProviderRouter
from agents.generation.generation_adapter import GenerationAdapter

class ExecutionGateway:

    def __init__(self, *, provider_store_service=None, provider_store_adapter=None):
        self.router = UnifiedProviderRouter()
        self.status = "READY"
        self.adapter = GenerationAdapter(
            provider_store_service=provider_store_service,
            provider_store_adapter=provider_store_adapter,
        )

    def execute(self, capability, payload=None):
        route = self.router.route(capability)

        if capability == "text_to_image" and payload and payload.get("prompt") and payload.get("output_path"):
            return self.adapter.generate_image(
                payload["prompt"],
                payload["output_path"],
                request_id=payload.get("request_id"),
                project_id=payload.get("project_id", "default"),
                estimated_cost=payload.get("estimated_cost", 0.0),
                route_options=payload.get("route_options"),
                adapter_options=payload.get("adapter_options"),
            )

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
