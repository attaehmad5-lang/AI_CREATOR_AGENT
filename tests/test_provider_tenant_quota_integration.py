"""ProviderService and routing integration for tenant-scoped provider quotas."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.routing_manager import ProviderRoutingManager
from projects.provider_store.provider_tenant_isolation import ProviderTenantIsolationManager


class AllowAuthorizationManager:
    def authorize(self, principal_id, operation):
        return {
            "allowed": isinstance(principal_id, str) and bool(principal_id),
            "principal_id": principal_id,
            "operation": operation,
        }


class TenantCapturingRouter:
    def __init__(self):
        self.route_options = None

    def route(self, **kwargs):
        self.route_options = dict(kwargs)
        return {"allowed": False, "status": "TEST_ROUTE_CAPTURED"}


class ProviderTenantQuotaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tenant_manager = ProviderTenantIsolationManager(self.root / "tenants.json")
        self.tenant_manager.register_tenant("tenant-a")
        self.tenant_manager.bind_principal("tenant-a", "user-a")
        self.tenant_manager.bind_project("tenant-a", "project-a")
        self.quota = ProviderQuotaManager(
            self.root / "quota.json",
            limits={"image": {"hourly": 5, "daily": 10}},
            tenant_limits={"tenant-a": {"image": {"hourly": 1, "daily": 2}}},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_provider_service_propagates_tenant_into_routing_manager(self):
        router = TenantCapturingRouter()
        service = ProviderService(
            connection_manager=type("Connection", (), {
                "verify_connectivity": lambda *_args, **_kwargs: {"verified": False}
            })(),
            routing_manager=router,
            quota_manager=self.quota,
            authorization_manager=AllowAuthorizationManager(),
            tenant_isolation_manager=self.tenant_manager,
        )
        result = service.execute_routed_request(
            category="image",
            project_id="project-a",
            request_id="route-tenant",
            estimated_cost=0,
            adapter=lambda: self.fail("adapter must not run when router denies"),
            modality="image",
            principal_id="user-a",
            tenant_id="tenant-a",
        )
        self.assertEqual(result["status"], "ROUTING_REJECTED")
        self.assertEqual(router.route_options["tenant_id"], "tenant-a")
        self.assertEqual(router.route_options["project_id"], "project-a")

    def test_provider_service_quota_finalization_is_tenant_bound(self):
        service = ProviderService(
            connection_manager=type("Connection", (), {
                "verify_connectivity": lambda *_args, **_kwargs: {"verified": False}
            })(),
            quota_manager=self.quota,
            authorization_manager=AllowAuthorizationManager(),
            tenant_isolation_manager=self.tenant_manager,
        )
        self.quota.reserve(
            provider_id="image", request_id="tenant-finalize", tenant_id="tenant-a",
        )
        denied = service.finalize_started_request(
            project_id="project-a", provider_id="image",
            request_id="tenant-finalize", estimated_cost=0, actual_cost=0,
            success=True, tenant_id="tenant-b",
        )
        self.assertEqual(denied["quota"]["status"], "TENANT_ISOLATION_DENIED")
        self.assertFalse(denied["finalized"])

        approved = service.finalize_started_request(
            project_id="project-a", provider_id="image",
            request_id="tenant-finalize", estimated_cost=0, actual_cost=0,
            success=True, tenant_id="tenant-a",
        )
        self.assertEqual(approved["quota"]["status"], "COMMITTED")
        self.assertTrue(approved["finalized"])

    def test_provider_service_releases_unstarted_reservation_with_same_tenant(self):
        service = ProviderService(
            connection_manager=type("Connection", (), {
                "verify_connectivity": lambda *_args, **_kwargs: {"verified": False}
            })(),
            quota_manager=self.quota,
            authorization_manager=AllowAuthorizationManager(),
            tenant_isolation_manager=self.tenant_manager,
        )
        self.quota.reserve(
            provider_id="image", request_id="tenant-release", tenant_id="tenant-a",
        )
        missing = service.release_unstarted_request(
            project_id="project-a", provider_id="image",
            request_id="tenant-release",
        )
        self.assertEqual(missing["status"], "TENANT_CONTEXT_REQUIRED")
        self.assertFalse(missing["released"])
        released = service.release_unstarted_request(
            project_id="project-a", provider_id="image",
            request_id="tenant-release", tenant_id="tenant-a",
        )
        self.assertTrue(released["released"])
        self.assertEqual(released["quota"]["status"], "RELEASED")

    def test_real_router_reserves_quota_under_tenant_identity(self):
        catalog = self.root / "providers.json"
        state = self.root / "connections.json"
        catalog.write_text(json.dumps({
            "providers": [{
                "id": "local-image",
                "name": "Local Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "API",
                "enabled": True,
                "zero_cost_allowed": True,
                "priority": 1,
                "capabilities": ["image"],
            }]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        connections = ConnectionManager(catalog, state)
        verified = connections.verify_connectivity(
            "local-image", lambda provider, **kwargs: {"ok": True}
        )
        self.assertTrue(verified["verified"])
        router = ProviderRoutingManager(
            connections, quota_manager=self.quota,
        )
        first = router.route(
            category="image", project_id="project-a",
            request_id="routed-a1", tenant_id="tenant-a",
        )
        self.assertTrue(first["allowed"])
        self.assertEqual(first["provider"], "local-image")
        self.assertTrue(first["quota_reserved"])
        second = router.route(
            category="image", project_id="project-a",
            request_id="routed-a2", tenant_id="tenant-a",
        )
        self.assertFalse(second["allowed"])
        self.assertEqual(second["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(self.quota.tenant_usage(
            tenant_id="tenant-a", provider_id="local-image"
        )["hourly"], 1)

    def test_router_cannot_bypass_tenant_policy_without_tenant_context(self):
        catalog = self.root / "providers-no-tenant.json"
        state = self.root / "connections-no-tenant.json"
        catalog.write_text(json.dumps({
            "providers": [{
                "id": "local-image",
                "name": "Local Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "API",
                "enabled": True,
                "zero_cost_allowed": True,
                "priority": 1,
                "capabilities": ["image"],
            }]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        connections = ConnectionManager(catalog, state)
        connections.verify_connectivity(
            "local-image", lambda provider, **kwargs: {"ok": True}
        )
        router = ProviderRoutingManager(connections, quota_manager=self.quota)
        result = router.route(
            category="image", project_id="project-a", request_id="no-tenant",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(self.quota.data["requests"], {})


if __name__ == "__main__":
    unittest.main()
