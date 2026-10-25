import tempfile
import unittest
from pathlib import Path

from projects.provider_store.tenant_policy_manager import ProviderTenantPolicyManager


class ProviderTenantPolicyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "tenant-policies.json"
        self.manager = ProviderTenantPolicyManager(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_policy_round_trips_without_credentials(self):
        policy = {
            "allowed_providers": ["primary"],
            "denied_providers": ["blocked"],
            "allowed_capabilities": ["image"],
            "allowed_models": ["sdxl"],
            "max_estimated_cost": 2.0,
            "automatic_fallback": False,
            "require_health_evidence": True,
            "require_verified_connection": True,
            "required_region": "SAUDI-ARABIA",
        }
        saved = self.manager.set_policy("tenant-a", policy)
        self.assertTrue(saved["saved"])
        loaded = ProviderTenantPolicyManager(self.path).get_policy("tenant-a")
        self.assertEqual(loaded["status"], "TENANT_POLICY_FOUND")
        self.assertEqual(loaded["policy"], policy)
        self.assertNotIn("token", repr(loaded).casefold())
        self.assertEqual(set(self.manager.data), {"version", "tenants"})

    def test_missing_invalid_and_removed_policies_are_explicit(self):
        self.assertEqual(self.manager.get_policy("missing")["status"], "TENANT_POLICY_NOT_FOUND")
        self.assertEqual(self.manager.get_policy("tenant with spaces")["status"], "INVALID_TENANT_ID")
        self.assertTrue(self.manager.set_policy("tenant-a", {"allowed_providers": ["primary"]})["saved"])
        self.assertTrue(self.manager.remove_policy("tenant-a")["removed"])
        self.assertEqual(self.manager.get_policy("tenant-a")["status"], "TENANT_POLICY_NOT_FOUND")

    def test_invalid_policy_and_corrupt_store_fail_closed(self):
        result = self.manager.set_policy("tenant-a", {"required_region": "region with spaces"})
        self.assertEqual(result["status"], "INVALID_TENANT_POLICY")
        self.assertFalse(result["saved"])
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderTenantPolicyManager(self.path)


if __name__ == "__main__":
    unittest.main()
