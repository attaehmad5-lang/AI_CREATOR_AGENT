import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_tenant_policy import ProviderTenantPolicyManager


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


class ProviderTenantScopeServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProviderTenantPolicyManager(Path(self.temp_dir.name) / "tenant.json")
        self.manager.register_tenant("tenant-a")
        self.manager.register_tenant("tenant-b")
        self.manager.bind_project(
            tenant_id="tenant-a", project_id="project-a",
            allowed_providers=["provider-safe"], allowed_modalities=["image"],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cross_tenant_request_is_blocked_before_routing(self):
        router = _Router("provider-safe")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            tenant_policy_manager=self.manager,
        )
        calls = []
        result = service.execute_routed_request(
            tenant_id="tenant-b", category="image", project_id="project-a",
            request_id="tenant-denied", estimated_cost=0.0,
            adapter=lambda **kwargs: calls.append(kwargs), modality="image",
        )
        self.assertEqual(result["status"], "TENANT_SCOPE_DENIED")
        self.assertEqual(router.calls, 0)
        self.assertEqual(calls, [])

    def test_selected_provider_is_rechecked_before_adapter_execution(self):
        router = _Router("provider-unsafe")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            tenant_policy_manager=self.manager,
        )
        calls = []
        result = service.execute_routed_request(
            tenant_id="tenant-a", category="image", project_id="project-a",
            request_id="provider-denied", estimated_cost=0.0,
            adapter=lambda **kwargs: calls.append(kwargs), modality="image",
        )
        self.assertEqual(result["status"], "TENANT_SCOPE_DENIED")
        self.assertEqual(router.calls, 1)
        self.assertEqual(calls, [])

    def test_missing_tenant_id_fails_closed(self):
        router = _Router("provider-safe")
        service = ProviderService(
            _ConnectionManager(), routing_manager=router,
            tenant_policy_manager=self.manager,
        )
        result = service.execute_routed_request(
            category="image", project_id="project-a", request_id="missing-tenant",
            estimated_cost=0.0, adapter=lambda **kwargs: {"started": False},
            modality="image",
        )
        self.assertEqual(result["status"], "TENANT_ID_REQUIRED")
        self.assertEqual(router.calls, 0)


if __name__ == "__main__":
    unittest.main()
