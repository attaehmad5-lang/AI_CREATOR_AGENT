"""Durable provider tenant-isolation and execution-gate regression tests."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_tenant_isolation import ProviderTenantIsolationManager


class FakeConnectionManager:
    def verify_connectivity(self, *_args, **_kwargs):
        return {"connected": False}


class StopAfterTenantGateRoutingManager:
    def __init__(self):
        self.calls = 0

    def route(self, **_kwargs):
        self.calls += 1
        return {"allowed": False, "status": "TEST_STOP_AFTER_TENANT_GATE"}


class ProviderTenantIsolationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "tenant-state.json"
        self.manager = ProviderTenantIsolationManager(self.state_path)
        for tenant, user, project in (
            ("tenant-a", "user-a", "project-a"),
            ("tenant-b", "user-b", "project-b"),
        ):
            self.assertTrue(self.manager.register_tenant(tenant)["saved"])
            self.assertTrue(self.manager.bind_principal(tenant, user)["saved"])
            self.assertTrue(self.manager.bind_project(tenant, project)["saved"])
        self.assertTrue(
            self.manager.bind_credential_ref("tenant-a", "vault://tenant-a/provider-key-1")["saved"]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_project_access_requires_matching_tenant_owner_and_member(self):
        allowed = self.manager.authorize_project("tenant-a", "project-a", "user-a")
        self.assertTrue(allowed["allowed"])
        for args in (
            ("tenant-b", "project-a", "user-b"),
            ("tenant-a", "project-b", "user-a"),
            ("tenant-a", "project-a", "user-b"),
            ("missing", "project-a", "user-a"),
            ("tenant-a", "project-a", None),
        ):
            with self.subTest(args=args):
                result = self.manager.authorize_project(*args)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["status"], "TENANT_ISOLATION_DENIED" if args[0] != "missing" and args[2] is not None else "INVALID_TENANT_CONTEXT" if args[2] is None else "TENANT_ISOLATION_DENIED")

    def test_credential_reference_access_is_tenant_and_principal_scoped(self):
        self.assertTrue(
            self.manager.authorize_credential(
                "tenant-a", "vault://tenant-a/provider-key-1", "user-a"
            )["allowed"]
        )
        self.assertFalse(
            self.manager.authorize_credential(
                "tenant-b", "vault://tenant-a/provider-key-1", "user-b"
            )["allowed"]
        )
        self.assertFalse(
            self.manager.authorize_credential(
                "tenant-a", "vault://tenant-a/provider-key-1", "user-b"
            )["allowed"]
        )

    def test_project_and_credential_references_cannot_be_rebound_to_another_tenant(self):
        self.assertEqual(
            self.manager.bind_project("tenant-b", "project-a")["status"],
            "PROJECT_ALREADY_OWNED",
        )
        self.assertEqual(
            self.manager.bind_credential_ref(
                "tenant-b", "vault://tenant-a/provider-key-1"
            )["status"],
            "CREDENTIAL_REFERENCE_ALREADY_OWNED",
        )

    def test_state_persists_and_stores_only_opaque_credential_references(self):
        reloaded = ProviderTenantIsolationManager(self.state_path)
        self.assertTrue(reloaded.authorize_project("tenant-a", "project-a", "user-a")["allowed"])
        summary = reloaded.tenant_summary("tenant-a")["summary"]
        self.assertEqual(summary["principal_count"], 1)
        self.assertEqual(summary["project_count"], 1)
        self.assertEqual(summary["credential_reference_count"], 1)
        raw = self.state_path.read_text(encoding="utf-8")
        self.assertIn("vault://tenant-a/provider-key-1", raw)
        self.assertNotIn("real-api-key-material", raw)

    def test_removing_principal_revokes_project_and_credential_access(self):
        result = self.manager.remove_principal("tenant-a", "user-a")
        self.assertTrue(result["saved"])
        self.assertFalse(self.manager.authorize_project("tenant-a", "project-a", "user-a")["allowed"])
        self.assertFalse(
            self.manager.authorize_credential(
                "tenant-a", "vault://tenant-a/provider-key-1", "user-a"
            )["allowed"]
        )

    def test_invalid_inputs_and_capacity_limits_fail_closed(self):
        self.assertEqual(self.manager.register_tenant("../bad")["status"], "INVALID_TENANT_ID")
        self.assertEqual(self.manager.bind_project("tenant-a", "../bad")["status"], "INVALID_PROJECT_ID")
        self.assertEqual(
            self.manager.bind_credential_ref("tenant-a", "bad ref")["status"],
            "INVALID_CREDENTIAL_REFERENCE",
        )
        limited = ProviderTenantIsolationManager(
            Path(self.temp.name) / "limited.json",
            max_tenants=1, max_projects=1,
            max_credential_refs=1, max_principals_per_tenant=1,
        )
        self.assertTrue(limited.register_tenant("tenant-one")["saved"])
        self.assertEqual(limited.register_tenant("tenant-two")["status"], "TENANT_CAPACITY_REACHED")
        self.assertTrue(limited.bind_principal("tenant-one", "user-one")["saved"])
        self.assertEqual(
            limited.bind_principal("tenant-one", "user-two")["status"],
            "TENANT_PRINCIPAL_CAPACITY_REACHED",
        )

    def test_concurrent_project_binding_has_exactly_one_tenant_owner(self):
        def bind(args):
            return self.manager.bind_project(*args)["status"]
        attempts = [
            ("tenant-a", "shared-project"),
            ("tenant-b", "shared-project"),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(bind, attempts))
        self.assertEqual(results.count("PROJECT_BOUND"), 1)
        self.assertEqual(
            results.count("PROJECT_ALREADY_OWNED") + results.count("PROJECT_ALREADY_BOUND"),
            1,
        )
        reloaded = ProviderTenantIsolationManager(self.state_path)
        owner_a = reloaded.authorize_project("tenant-a", "shared-project", "user-a")["allowed"]
        owner_b = reloaded.authorize_project("tenant-b", "shared-project", "user-b")["allowed"]
        self.assertNotEqual(owner_a, owner_b)

    def test_corrupt_or_inconsistent_state_is_rejected(self):
        bad_state = Path(self.temp.name) / "bad-state.json"
        bad_state.write_text(json.dumps({
            "version": "1.0",
            "tenants": {
                "tenant-a": {
                    "principal_ids": [["unhashable"]],
                    "project_ids": [],
                    "credential_refs": [],
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
            "project_owners": {},
            "credential_owners": {},
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderTenantIsolationManager(bad_state)

        inconsistent = Path(self.temp.name) / "inconsistent.json"
        inconsistent.write_text(json.dumps({
            "version": "1.0",
            "tenants": {
                "tenant-a": {
                    "principal_ids": ["user-a"],
                    "project_ids": ["project-a"],
                    "credential_refs": [],
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
            "project_owners": {"project-a": "tenant-b"},
            "credential_owners": {},
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderTenantIsolationManager(inconsistent)


class ProviderServiceTenantGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = ProviderTenantIsolationManager(Path(self.temp.name) / "tenant.json")
        self.manager.register_tenant("tenant-a")
        self.manager.bind_principal("tenant-a", "user-a")
        self.manager.bind_project("tenant-a", "project-a")
        self.routing = StopAfterTenantGateRoutingManager()
        self.service = ProviderService(
            FakeConnectionManager(),
            routing_manager=self.routing,
            tenant_isolation_manager=self.manager,
        )
        self.adapter_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, *, tenant_id, principal_id, project_id="project-a"):
        return self.service.execute_routed_request(
            category="text",
            project_id=project_id,
            request_id="request-tenant-test",
            estimated_cost=0,
            adapter=lambda: self.adapter_calls.append("called"),
            modality="text",
            principal_id=principal_id,
            tenant_id=tenant_id,
        )

    def test_missing_tenant_context_is_rejected_before_routing(self):
        result = self.execute(tenant_id=None, principal_id="user-a")
        self.assertEqual(result["status"], "TENANT_CONTEXT_REQUIRED")
        self.assertEqual(self.routing.calls, 0)
        self.assertEqual(self.adapter_calls, [])

    def test_cross_tenant_project_access_is_rejected_before_routing_or_adapter(self):
        result = self.execute(tenant_id="tenant-b", principal_id="user-b")
        self.assertEqual(result["status"], "TENANT_ISOLATION_DENIED")
        self.assertFalse(result["executed"])
        self.assertEqual(self.routing.calls, 0)
        self.assertEqual(self.adapter_calls, [])

    def test_valid_tenant_context_passes_gate_before_normal_routing(self):
        result = self.execute(tenant_id="tenant-a", principal_id="user-a")
        self.assertEqual(result["status"], "ROUTING_REJECTED")
        self.assertEqual(self.routing.calls, 1)
        self.assertEqual(self.adapter_calls, [])

    def test_tenant_manager_contract_is_required_when_configured(self):
        with self.assertRaises(ValueError):
            ProviderService(FakeConnectionManager(), tenant_isolation_manager=object())


if __name__ == "__main__":
    unittest.main()
