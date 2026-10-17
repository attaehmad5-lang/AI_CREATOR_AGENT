"""Tenant/project/provider scope policy tests."""
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_tenant_policy import ProviderTenantPolicyManager


class ProviderTenantPolicyManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tenant-policy.json"
        self.manager = ProviderTenantPolicyManager(self.path)
        self.manager.register_tenant("tenant-a")
        self.manager.register_tenant("tenant-b")

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, tenant="tenant-a", project="project-a", providers=None, modalities=None, enabled=True):
        return self.manager.bind_project(
            tenant_id=tenant, project_id=project,
            allowed_providers=["image"] if providers is None else providers,
            allowed_modalities=["image"] if modalities is None else modalities,
            enabled=enabled,
        )

    def test_scope_requires_tenant_project_provider_and_modality_match(self):
        self.assertTrue(self.bind()["saved"])
        allowed = self.manager.authorize_scope(
            tenant_id="tenant-a", project_id="project-a", modality="image", provider_id="image"
        )
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["status"], "TENANT_SCOPE_ALLOWED")

    def test_cross_tenant_project_access_is_denied(self):
        self.bind()
        result = self.manager.authorize_scope(
            tenant_id="tenant-b", project_id="project-a", modality="image", provider_id="image"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PROJECT_NOT_BOUND_TO_TENANT")

    def test_project_cannot_be_bound_to_two_tenants(self):
        self.assertTrue(self.bind()["saved"])
        result = self.bind(tenant="tenant-b", project="project-a")
        self.assertEqual(result["status"], "PROJECT_ALREADY_BOUND_TO_ANOTHER_TENANT")

    def test_provider_and_modality_allowlists_are_enforced(self):
        self.bind(providers=["image"], modalities=["image"])
        denied_provider = self.manager.authorize_scope(
            tenant_id="tenant-a", project_id="project-a", modality="image", provider_id="voice"
        )
        denied_modality = self.manager.authorize_scope(
            tenant_id="tenant-a", project_id="project-a", modality="audio", provider_id="image"
        )
        self.assertEqual(denied_provider["status"], "PROVIDER_NOT_ALLOWED")
        self.assertEqual(denied_modality["status"], "MODALITY_NOT_ALLOWED")

    def test_disabled_tenant_and_project_fail_closed(self):
        self.bind()
        self.manager.register_tenant("tenant-a", enabled=False)
        self.assertEqual(
            self.manager.authorize_scope(
                tenant_id="tenant-a", project_id="project-a", modality="image"
            )["status"],
            "TENANT_DISABLED",
        )
        self.manager.register_tenant("tenant-a", enabled=True)
        self.bind(enabled=False)
        self.assertEqual(
            self.manager.authorize_scope(
                tenant_id="tenant-a", project_id="project-a", modality="image"
            )["status"],
            "PROJECT_DISABLED",
        )

    def test_state_persists_and_reloads(self):
        self.bind()
        loaded = ProviderTenantPolicyManager(self.path)
        self.assertTrue(loaded.authorize_scope(
            tenant_id="tenant-a", project_id="project-a", modality="image", provider_id="image"
        )["allowed"])

    def test_invalid_scope_and_policy_inputs_are_rejected(self):
        self.assertFalse(self.bind(providers=["image", "image"])["saved"])
        self.assertEqual(
            self.manager.authorize_scope(
                tenant_id=[], project_id="project-a", modality="image"
            )["status"],
            "INVALID_TENANT_SCOPE",
        )
        self.assertEqual(self.bind(tenant="missing")["status"], "TENANT_NOT_FOUND")

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderTenantPolicyManager(self.path)


if __name__ == "__main__":
    unittest.main()
