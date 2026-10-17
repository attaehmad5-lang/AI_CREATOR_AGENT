import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager
from projects.provider_store.provider_service import ProviderService


class _ConnectionManager:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class _Router:
    def __init__(self, provider_id):
        self.provider_id = provider_id
        self.calls = 0

    def route(self, **kwargs):
        self.calls += 1
        return {"allowed": True, "provider": self.provider_id, "billing_mode": "ZERO_COST"}


class ProviderDegradedModeServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProviderDegradedModeManager(Path(self.temp_dir.name) / "degraded.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fail_closed_mode_blocks_before_routing_or_adapter_invocation(self):
        self.manager.set_mode(
            project_id="project-a", service_id="image",
            mode="FAIL_CLOSED", reason_code="OUTAGE",
        )
        router = _Router("provider-a")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            degraded_mode_manager=self.manager,
        )
        adapter_calls = []
        result = service.execute_routed_request(
            category="image", project_id="project-a", request_id="request-a",
            estimated_cost=0.0, adapter=lambda **kwargs: adapter_calls.append(kwargs),
            modality="image",
        )
        self.assertEqual(result["status"], "DEGRADED_MODE_BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(router.calls, 0)
        self.assertEqual(adapter_calls, [])

    def test_degraded_mode_rechecks_selected_provider_after_routing(self):
        self.manager.set_mode(
            project_id="project-a", service_id="image",
            mode="DEGRADED", reason_code="PARTIAL_OUTAGE",
            allowed_providers=["provider-safe"],
        )
        router = _Router("provider-not-allowed")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            degraded_mode_manager=self.manager,
        )
        adapter_calls = []
        result = service.execute_routed_request(
            category="image", project_id="project-a", request_id="request-b",
            estimated_cost=0.0, adapter=lambda **kwargs: adapter_calls.append(kwargs),
            modality="image",
        )
        self.assertEqual(result["status"], "DEGRADED_MODE_BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(router.calls, 1)
        self.assertEqual(adapter_calls, [])
        self.assertEqual(result["release"]["status"], "RESERVATION_MANAGERS_UNAVAILABLE")

    def test_degraded_mode_requires_a_persisted_policy(self):
        router = _Router("provider-a")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            degraded_mode_manager=self.manager,
        )
        result = service.execute_routed_request(
            category="image", project_id="project-a", request_id="request-c",
            estimated_cost=0.0, adapter=lambda **kwargs: {"started": False, "success": False},
            modality="image",
        )
        self.assertEqual(result["status"], "DEGRADED_MODE_BLOCKED")
        self.assertEqual(router.calls, 0)


if __name__ == "__main__":
    unittest.main()
