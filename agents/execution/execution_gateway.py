import sys
sys.path.insert(0, '/storage/emulated/0/AI_CREATOR_AGENT')
from projects.provider_store.unified_provider_router import UnifiedProviderRouter
from agents.generation.generation_adapter import GenerationAdapter

class ExecutionGateway:

    def __init__(self, router=None, policy_executor=None):
        self.router = router or UnifiedProviderRouter()
        self.status = "READY"
        self.adapter = GenerationAdapter()
        self.policy_executor = policy_executor

    def execute(self, capability, payload=None):
        # The compatibility router is discovery-only: it does not select a provider
        # or reserve credits, cost, or quota. Never bypass the policy-aware execution
        # service by calling the legacy adapter directly.
        if capability == "text_to_image" and payload and payload.get("prompt") and payload.get("output_path"):
            if not callable(getattr(self.policy_executor, "execute", None)):
                route = self.router.route(capability)
                return {
                    "status": "POLICY_AWARE_EXECUTOR_REQUIRED",
                    "capability": capability,
                    "executed": False,
                    "route": route,
                    "reason": "Discovery routing cannot authorize billable generation.",
                }
            try:
                result = self.policy_executor.execute(payload)
            except Exception as exc:
                return {
                    "status": "POLICY_EXECUTION_FAILED",
                    "capability": capability,
                    "executed": False,
                    "error_type": type(exc).__name__,
                }
            return result if isinstance(result, dict) else {
                "status": "INVALID_POLICY_EXECUTOR_RESPONSE",
                "capability": capability,
                "executed": False,
            }

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
