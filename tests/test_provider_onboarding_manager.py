import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_onboarding_manager import ProviderOnboardingManager


class ProviderOnboardingManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "onboarding.json"
        self.catalog_path.write_text(
            json.dumps({"version": "3.0", "providers": []}),
            encoding="utf-8",
        )
        self.connection_manager = ConnectionManager(
            self.catalog_path, self.root / "connections.json"
        )
        self.probe_results = [{"verified": True, "status": "PROBE_OK"}]
        self.manager = ProviderOnboardingManager(
            self.state_path,
            self.connection_manager,
            authorize_admin=lambda principal_id: principal_id == "trusted-admin",
            probe_provider=lambda manifest: self.probe_results.pop(0),
        )
        self.manifest = {
            "id": "new_image",
            "name": "New Image Provider",
            "category": "image",
            "auth_type": "API_KEY",
            "connection_type": "API",
            "official_api_url": "https://api.example.com/v1",
            "official_docs_url": "https://docs.example.com/api",
            "allowed_hosts": ["api.example.com"],
            "capabilities": ["image_generation"],
            "adapter_api_version": "1.0",
            "subscription_required": True,
            "enabled": False,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_submission_is_disabled_and_has_no_catalog_side_effect(self):
        submitted = self.manager.submit(self.manifest)
        self.assertEqual(submitted["status"], "ONBOARDING_SUBMITTED")
        self.assertTrue(submitted["accepted"])
        self.assertIsNone(self.connection_manager.get_provider("new_image"))
        self.assertEqual(
            self.manager.get_status("new_image")["onboarding_status"],
            "PENDING_APPROVAL",
        )

    def test_approval_probe_and_activation_are_separate_gated_transitions(self):
        self.assertEqual(
            self.manager.activate("new_image", principal_id="trusted-admin")["status"],
            "ONBOARDING_NOT_FOUND",
        )
        self.manager.submit(self.manifest)
        denied = self.manager.approve("new_image", principal_id="ordinary-user")
        self.assertEqual(denied["status"], "ADMIN_APPROVAL_REQUIRED")
        approved = self.manager.approve("new_image", principal_id="trusted-admin")
        self.assertTrue(approved["approved"])
        probe = self.manager.verify("new_image", principal_id="trusted-admin")
        self.assertTrue(probe["verified"])
        activated = self.manager.activate("new_image", principal_id="trusted-admin")
        self.assertTrue(activated["activated"])
        self.assertEqual(activated["status"], "PROVIDER_ONBOARDED_DISABLED")
        provider = self.connection_manager.get_provider("new_image")
        self.assertIsNotNone(provider)
        self.assertFalse(provider["enabled"])

    def test_provider_cannot_be_registered_before_successful_live_probe(self):
        self.manager.submit(self.manifest)
        self.manager.approve("new_image", principal_id="trusted-admin")
        self.probe_results[0] = {"verified": False, "status": "AUTH_FAILED"}
        failed = self.manager.verify("new_image", principal_id="trusted-admin")
        self.assertFalse(failed["verified"])
        self.assertEqual(failed["probe_status"], "AUTH_FAILED")
        self.assertEqual(
            self.manager.activate("new_image", principal_id="trusted-admin")["status"],
            "LIVE_PROBE_REQUIRED",
        )
        self.assertIsNone(self.connection_manager.get_provider("new_image"))

    def test_failed_probe_can_be_retried_after_remediation(self):
        self.manager.submit(self.manifest)
        self.manager.approve("new_image", principal_id="trusted-admin")
        self.probe_results[0] = {"verified": False, "status": "TEMPORARY_FAILURE"}
        self.assertFalse(self.manager.verify("new_image", principal_id="trusted-admin")["verified"])
        self.probe_results.append({"verified": True, "status": "PROBE_OK"})
        self.assertTrue(self.manager.verify("new_image", principal_id="trusted-admin")["verified"])

    def test_manifest_rejects_secret_values_and_unsafe_urls_or_hosts(self):
        secret_manifest = dict(self.manifest, api_key="literal-secret")
        self.assertEqual(
            self.manager.submit(secret_manifest)["status"],
            "SENSITIVE_METADATA_REJECTED",
        )
        unsafe = dict(self.manifest, official_api_url="http://api.example.com/v1")
        self.assertEqual(self.manager.submit(unsafe)["status"], "OFFICIAL_HTTPS_URLS_REQUIRED")
        wrong_host = dict(self.manifest, allowed_hosts=["other.example.com"])
        self.assertEqual(self.manager.submit(wrong_host)["status"], "INVALID_API_HOST_ALLOWLIST")
        enabled = dict(self.manifest, enabled=True)
        self.assertEqual(self.manager.submit(enabled)["status"], "ONBOARDING_MUST_START_DISABLED")

    def test_onboarding_state_persists_without_probe_payload_or_credentials(self):
        self.manager.submit(self.manifest)
        reopened = ProviderOnboardingManager(
            self.state_path,
            self.connection_manager,
            authorize_admin=lambda principal_id: principal_id == "trusted-admin",
            probe_provider=lambda manifest: {"verified": False, "status": "NOOP"},
        )
        self.assertEqual(
            reopened.get_status("new_image")["onboarding_status"],
            "PENDING_APPROVAL",
        )
        stored = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("api_key", stored)
        self.assertNotIn("PROBE_OK", stored)


if __name__ == "__main__":
    unittest.main()
