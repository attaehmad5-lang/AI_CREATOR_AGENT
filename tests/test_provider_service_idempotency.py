import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_request_idempotency import ProviderRequestIdempotencyManager
from projects.provider_store.provider_service import ProviderService


class _Router:
    def __init__(self):
        self.calls = 0
        self.last_request_id = None

    def route(self, **kwargs):
        self.calls += 1
        self.last_request_id = kwargs.get("request_id")
        return {
            "allowed": True, "status": "ROUTED",
            "provider": "image", "billing_mode": "ZERO_COST",
        }


class ProviderServiceIdempotencyIntegrationTests(unittest.TestCase):
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
        self.assertTrue(
            self.connections.verify_connectivity(
                "image", lambda provider, **kwargs: {"ok": True},
            )["verified"]
        )
        self.idempotency = ProviderRequestIdempotencyManager(
            self.root / "idempotency.json", lease_seconds=30,
        )
        self.router = _Router()
        self.service = ProviderService(
            self.connections, routing_manager=self.router,
            request_idempotency_manager=self.idempotency,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_provider_outcome_cannot_be_executed_twice(self):
        calls = []

        def adapter(**kwargs):
            calls.append(kwargs["request_id"])
            raise RuntimeError("provider may have accepted request")

        first = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="stable-request-1", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        second = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="stable-request-1", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        self.assertEqual(first["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(second["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.router.calls, 1)
        status = self.idempotency.get_status(
            project_id="film", request_id="stable-request-1", tenant_id="tenant-a",
        )
        self.assertEqual(status["state"], "UNKNOWN")

    def test_same_request_id_with_changed_payload_fingerprint_is_rejected(self):
        calls = []

        def adapter(**kwargs):
            calls.append(kwargs["request_id"])
            raise RuntimeError("ambiguous provider result")

        self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="stable-request-2", estimated_cost=0.0,
            adapter=adapter, modality="image",
            adapter_options={"prompt": "first prompt"},
        )
        conflict = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="stable-request-2", estimated_cost=0.0,
            adapter=adapter, modality="image",
            adapter_options={"prompt": "different prompt"},
        )
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.router.calls, 1)

    def test_adapter_confirmed_not_started_allows_safe_retry(self):
        calls = []

        def adapter(**kwargs):
            calls.append(kwargs["request_id"])
            return {
                "started": False, "success": False,
                "charge_outcome": "not_incurred",
            }

        first = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="safe-retry-request", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        second = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            request_id="safe-retry-request", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        self.assertIn(first["status"], {"EXECUTION_NOT_STARTED", "RESERVATION_RELEASE_FAILED"})
        self.assertIn(second["status"], {"EXECUTION_NOT_STARTED", "RESERVATION_RELEASE_FAILED"})
        self.assertEqual(len(calls), 2)
        status = self.idempotency.get_status(
            project_id="film", request_id="safe-retry-request", tenant_id="tenant-a",
        )
        self.assertEqual(status["state"], "NOT_STARTED")
        self.assertEqual(status["attempts"], 2)

    def test_generated_request_id_is_reserved_and_returned(self):
        def adapter(**kwargs):
            raise RuntimeError("ambiguous")

        result = self.service.execute_routed_request(
            category="image", project_id="film", tenant_id="tenant-a",
            estimated_cost=0.0, adapter=adapter, modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertTrue(result["request_id"])
        status = self.idempotency.get_status(
            project_id="film", request_id=result["request_id"], tenant_id="tenant-a",
        )
        self.assertEqual(status["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
