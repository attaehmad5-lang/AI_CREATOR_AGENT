import unittest

from agents.execution.execution_gateway import ExecutionGateway


class CanonicalServiceStub:
    def __init__(self):
        self.request = None

    def execute_routed_request(self, **request):
        self.request = request
        request["adapter"](provider_id="hf", request_id=request["request_id"])
        return {"status": "PROVIDER_EXECUTION_SUCCEEDED", "executed": True, "provider": "hf"}


class ExecutionGatewayProviderStoreTests(unittest.TestCase):
    def test_gateway_passes_request_context_to_canonical_generation_adapter(self):
        service = CanonicalServiceStub()
        calls = []
        gateway = ExecutionGateway(
            provider_store_service=service,
            provider_store_adapter=lambda **kwargs: calls.append(kwargs) or {
                "started": True, "success": True, "actual_cost": 0,
                "charge_outcome": "not_incurred",
            },
        )

        result = gateway.execute("text_to_image", {
            "prompt": "cinematic scene",
            "output_path": "/tmp/scene.png",
            "request_id": "gateway-request-1",
            "project_id": "documentary",
            "estimated_cost": 0.0,
            "route_options": {"capability": "text_to_image"},
        })

        self.assertEqual(result["status"], "PROVIDER_EXECUTION_SUCCEEDED")
        self.assertEqual(service.request["request_id"], "gateway-request-1")
        self.assertEqual(service.request["project_id"], "documentary")
        self.assertEqual(service.request["category"], "image")
        self.assertEqual(service.request["route_options"] if "route_options" in service.request else service.request.get("capability"), "text_to_image")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
