import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_request_trace import current_trace_context
from projects.provider_store.provider_service import ProviderService


class _RoutingManager:
    def __init__(self):
        self.calls = []

    def route(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "allowed": True, "status": "ROUTED",
            "provider": "image", "billing_mode": "ZERO_COST",
        }


class ProviderServiceTraceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        catalog = self.root / "providers.json"
        state = self.root / "connections.json"
        catalog.write_text(json.dumps({"providers": [{
            "id": "image", "name": "Image", "category": "image",
            "auth_type": "NONE", "connection_type": "API", "enabled": True,
            "zero_cost_allowed": True,
        }]}), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(catalog, state)
        verified = self.connections.verify_connectivity("image", lambda provider, **kwargs: {"ok": True})
        self.assertTrue(verified["verified"])
        self.router = _RoutingManager()
        self.service = ProviderService(self.connections, routing_manager=self.router)

    def tearDown(self):
        self.temp.cleanup()

    def test_execution_generates_unique_request_and_trace_ids_and_propagates_context(self):
        observed = {}

        def adapter(**kwargs):
            observed.update(current_trace_context())
            self.assertEqual(kwargs["request_id"], observed["request_id"])
            raise RuntimeError("simulated provider transport failure")

        result = self.service.execute_routed_request(
            category="image", project_id="film", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(result["request_id"], observed["request_id"])
        self.assertEqual(result["trace_id"], observed["trace_id"])
        self.assertEqual(result["traceparent"], observed["traceparent"])
        self.assertEqual(self.router.calls[0]["request_id"], result["request_id"])
        self.assertNotIn("trace_id", self.router.calls[0])
        self.assertNotIn("traceparent", self.router.calls[0])
        self.assertIsNone(current_trace_context())

    def test_execution_continues_incoming_w3c_trace_with_new_child_span(self):
        remote_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
        remote_span = "00f067aa0ba902b7"
        observed = {}

        def adapter(**kwargs):
            observed.update(current_trace_context())
            raise RuntimeError("simulated provider transport failure")

        result = self.service.execute_routed_request(
            category="image", project_id="film", request_id="stable-request-1",
            traceparent=f"00-{remote_trace}-{remote_span}-01",
            estimated_cost=0.0, adapter=adapter, modality="image",
        )
        self.assertEqual(result["request_id"], "stable-request-1")
        self.assertEqual(result["trace_id"], remote_trace)
        self.assertEqual(observed["parent_span_id"], remote_span)
        self.assertNotEqual(observed["span_id"], remote_span)
        self.assertEqual(observed["trace_id"], remote_trace)

    def test_direct_routing_also_generates_context_and_strips_trace_fields_from_router(self):
        result = self.service.route_request(
            category="image", project_id="film", estimated_cost=0.0,
        )
        self.assertTrue(result["allowed"])
        self.assertTrue(result["request_id"])
        self.assertRegex(result["trace_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(result["traceparent"], r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
        self.assertNotIn("trace_id", self.router.calls[0])
        self.assertNotIn("traceparent", self.router.calls[0])


if __name__ == "__main__":
    unittest.main()
