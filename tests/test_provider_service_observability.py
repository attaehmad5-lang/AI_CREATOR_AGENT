"""ProviderService integration tests for structured logs and bounded traces."""
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_structured_logger import ProviderStructuredLogger
from projects.provider_store.provider_trace_manager import ProviderTraceManager


class ProviderServiceObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        catalog = root / "providers.json"
        state = root / "connections.json"
        catalog.write_text(json.dumps({
            "providers": [{
                "id": "image", "name": "Image", "category": "image",
                "auth_type": "NONE", "connection_type": "API",
                "enabled": True, "zero_cost_allowed": True,
            }]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(catalog, state)
        verified = self.connections.verify_connectivity("image", lambda provider, **kwargs: {"ok": True})
        self.assertTrue(verified.get("verified"))
        self.stream = io.StringIO()
        self.logger = logging.Logger("provider-observability-test", level=logging.DEBUG)
        self.logger.addHandler(logging.StreamHandler(self.stream))
        self.structured = ProviderStructuredLogger(self.logger)
        self.traces = ProviderTraceManager()
        class RejectingRouter:
            def route(self, **kwargs):
                return {"allowed": False, "status": "NO_ELIGIBLE_PROVIDER"}
        self.router = RejectingRouter()
        self.service = ProviderService(
            self.connections, routing_manager=self.router,
            structured_logger=self.structured, trace_manager=self.traces,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verify_provider_emits_safe_event_and_finishes_trace(self):
        result = self.service.verify_provider(
            "image", lambda provider, **kwargs: {"ok": True},
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
        self.assertTrue(result["verified"])
        event = json.loads(self.stream.getvalue().splitlines()[-1])
        self.assertEqual(event["event"], "provider.verify")
        self.assertEqual(event["outcome"], "OK")
        self.assertEqual(event["provider_id"], "image")
        snapshot = self.traces.snapshot()
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["spans"][0]["outcome"], "OK")
        self.assertEqual(snapshot["spans"][0]["trace_id"], "a" * 32)

    def test_execute_routing_rejection_is_logged_and_traced_without_adapter_call(self):
        calls = []
        result = self.service.execute_routed_request(
            category="image", project_id="film", request_id="request-1",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: calls.append(kwargs),
        )
        self.assertEqual(result["status"], "ROUTING_REJECTED")
        self.assertEqual(calls, [])
        event = json.loads(self.stream.getvalue().splitlines()[-1])
        self.assertEqual(event["event"], "provider.execute")
        self.assertEqual(event["outcome"], "ERROR")
        self.assertEqual(event["request_id"], "request-1")
        self.assertEqual(event["error_code"], "ROUTING_REJECTED")
        snapshot = self.traces.snapshot()
        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["spans"][0]["outcome"], "ERROR")

    def test_observability_backend_failure_never_changes_provider_result(self):
        class BrokenLogger:
            def emit(self, *args, **kwargs):
                raise RuntimeError("private logger backend detail")
        class BrokenTrace:
            def start_span(self, *args, **kwargs):
                raise RuntimeError("private trace backend detail")
            def finish_span(self, *args, **kwargs):
                raise RuntimeError("private trace backend detail")
        service = ProviderService(
            self.connections, routing_manager=self.router,
            structured_logger=BrokenLogger(), trace_manager=BrokenTrace(),
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="request-2",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: self.fail("router rejected before adapter"),
        )
        self.assertEqual(result["status"], "ROUTING_REJECTED")
        self.assertNotIn("private", repr(result))

    def test_invalid_traceparent_does_not_block_provider_operation(self):
        result = self.service.execute_routed_request(
            category="image", project_id="film", request_id="request-3",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: self.fail("router rejected before adapter"),
            traceparent="not-a-traceparent",
        )
        self.assertEqual(result["status"], "ROUTING_REJECTED")
        self.assertEqual(self.traces.snapshot()["count"], 0)


if __name__ == "__main__":
    unittest.main()
