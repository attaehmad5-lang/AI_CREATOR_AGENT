import unittest

from agents.generation.generation_adapter import GenerationAdapter


class CanonicalServiceStub:
    def __init__(self, result=None, invoke=True, error=None):
        self.result = result or {"status": "PROVIDER_EXECUTION_SUCCEEDED", "executed": True, "provider": "provider-a"}
        self.invoke = invoke
        self.error = error
        self.request = None

    def execute_routed_request(self, **request):
        self.request = request
        if self.error:
            raise self.error
        if self.invoke:
            request["adapter"](provider_id="provider-a", request_id=request["request_id"])
        return self.result


class GenerationAdapterProviderStoreTests(unittest.TestCase):
    def test_image_generation_uses_canonical_service_when_injected(self):
        calls = []
        service = CanonicalServiceStub()
        adapter = GenerationAdapter(
            provider_store_service=service,
            provider_store_adapter=lambda **kwargs: calls.append(kwargs) or {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": kwargs["output_path"],
            },
        )

        result = adapter.generate_image(
            "cinematic landscape", "/tmp/output.png",
            request_id="image-req-1", project_id="film",
            route_options={"capability": "text_to_image"},
        )

        self.assertEqual(result["status"], "PROVIDER_EXECUTION_SUCCEEDED")
        self.assertEqual(service.request["request_id"], "image-req-1")
        self.assertEqual(service.request["project_id"], "film")
        self.assertEqual(service.request["modality"], "image")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["provider_id"], "provider-a")
        self.assertEqual(calls[0]["prompt"], "cinematic landscape")
        self.assertEqual(calls[0]["output_path"], "/tmp/output.png")

    def test_configured_service_fails_closed_without_executor(self):
        service = CanonicalServiceStub()
        adapter = GenerationAdapter(provider_store_service=service)

        result = adapter.generate_image("prompt", "/tmp/output.png")

        self.assertEqual(result["status"], "PROVIDER_EXECUTOR_UNAVAILABLE")
        self.assertFalse(result["executed"])
        self.assertIsNone(service.request)

    def test_route_option_collision_is_rejected_before_execution(self):
        service = CanonicalServiceStub()
        adapter = GenerationAdapter(
            provider_store_service=service,
            provider_store_adapter=lambda **kwargs: {"started": False},
        )
        result = adapter.generate_image(
            "prompt", "/tmp/output.png",
            route_options={"project_id": "attacker-overrides-project"},
        )
        self.assertEqual(result["status"], "INVALID_ROUTE_OPTIONS")
        self.assertFalse(result["executed"])
        self.assertIsNone(service.request)

    def test_invalid_estimated_cost_fails_before_service_call(self):
        service = CanonicalServiceStub()
        adapter = GenerationAdapter(
            provider_store_service=service,
            provider_store_adapter=lambda **kwargs: {},
        )
        for invalid in (-1, float("nan"), float("inf"), True, "0"):
            with self.subTest(estimated_cost=invalid):
                result = adapter.generate_image(
                    "prompt", "/tmp/output.png", estimated_cost=invalid
                )
                self.assertEqual(result["status"], "INVALID_ESTIMATED_COST")
                self.assertIsNone(service.request)

    def test_invalid_output_path_fails_before_service_call(self):
        service = CanonicalServiceStub()
        adapter = GenerationAdapter(
            provider_store_service=service,
            provider_store_adapter=lambda **kwargs: {},
        )
        for invalid in ("", "   ", "bad\x00path", None):
            with self.subTest(output_path=repr(invalid)):
                result = adapter.generate_image("prompt", invalid)
                self.assertEqual(result["status"], "INVALID_OUTPUT_PATH")
                self.assertIsNone(service.request)

    def test_invalid_canonical_service_fails_closed(self):
        adapter = GenerationAdapter(provider_store_service=object(), provider_store_adapter=lambda **kwargs: {})
        result = adapter.generate_image("prompt", "/tmp/output.png")
        self.assertEqual(result["status"], "PROVIDER_STORE_SERVICE_INVALID")
        self.assertFalse(result["executed"])

    def test_service_exception_is_reported_as_ambiguous_without_secret_details(self):
        service = CanonicalServiceStub(error=RuntimeError("private endpoint and token"))
        adapter = GenerationAdapter(provider_store_service=service, provider_store_adapter=lambda **kwargs: {})
        result = adapter.generate_image("prompt", "/tmp/output.png", request_id="image-req-4")

        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(result["reservation_state"], "UNKNOWN")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(result["request_id"], "image-req-4")
        self.assertNotIn("private endpoint", repr(result))
        self.assertNotIn("token", repr(result).lower())


if __name__ == "__main__":
    unittest.main()
