import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_concurrency_manager import ProviderConcurrencyManager
from projects.provider_store.provider_service import ProviderService


class _Router:
    def route(self, **kwargs):
        return {
            "allowed": True, "status": "ROUTED",
            "provider": "image", "billing_mode": "ZERO_COST",
        }


class ProviderServiceConcurrencyIntegrationTests(unittest.TestCase):
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
        self.concurrency = ProviderConcurrencyManager(
            self.root / "concurrency.json", provider_limits={"image": 1},
            lease_seconds=6, heartbeat_interval=0.5,
        )
        self.service = ProviderService(
            self.connections, routing_manager=_Router(),
            concurrency_manager=self.concurrency,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_provider_service_blocks_second_adapter_while_provider_slot_is_occupied(self):
        adapter_started = threading.Event()
        release_adapter = threading.Event()
        second_adapter_calls = []

        def slow_adapter(**kwargs):
            adapter_started.set()
            if not release_adapter.wait(timeout=5):
                raise RuntimeError("test adapter timed out")
            return {
                "started": False, "success": False,
                "charge_outcome": "not_incurred",
            }

        def second_adapter(**kwargs):
            second_adapter_calls.append(kwargs["request_id"])
            return {
                "started": False, "success": False,
                "charge_outcome": "not_incurred",
            }

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                self.service.execute_routed_request,
                category="image", project_id="film", request_id="request-one",
                tenant_id="tenant-a", estimated_cost=0.0,
                adapter=slow_adapter, modality="image",
            )
            self.assertTrue(adapter_started.wait(timeout=5))
            second = self.service.execute_routed_request(
                category="image", project_id="film", request_id="request-two",
                tenant_id="tenant-a", estimated_cost=0.0,
                adapter=second_adapter, modality="image",
            )
            self.assertEqual(second["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")
            self.assertEqual(second_adapter_calls, [])
            release_adapter.set()
            first = first_future.result(timeout=10)

        self.assertIn(first["status"], {"EXECUTION_NOT_STARTED", "RESERVATION_RELEASE_FAILED"})
        self.assertEqual(self.concurrency.get_status("image")["active"], 0)

    def test_concurrency_limit_failure_happens_before_adapter_start(self):
        lease = self.concurrency.acquire(
            provider_id="image", project_id="film", request_id="already-running",
            tenant_id="tenant-a",
        )
        self.assertTrue(lease["acquired"])
        calls = []

        def adapter(**kwargs):
            calls.append(kwargs["request_id"])
            return {"started": False, "success": False, "charge_outcome": "not_incurred"}

        result = self.service.execute_routed_request(
            category="image", project_id="film", request_id="blocked-request",
            tenant_id="tenant-a", estimated_cost=0.0,
            adapter=adapter, modality="image",
        )
        self.assertEqual(result["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")
        self.assertEqual(calls, [])
        self.concurrency.release(lease["lease_token"])


if __name__ == "__main__":
    unittest.main()
