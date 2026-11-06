"""Provider retention manager regression tests."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy
from projects.provider_store.provider_retention_manager import ProviderRetentionManager


class ProviderRetentionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "retention.json"
        policy = ProviderDataHandlingPolicy({
            "image": {
                "allowed_categories": ["public", "user_content"],
                "max_retention_days": 30,
                "training_use_allowed": False,
                "source_url": "https://provider.example/privacy",
            }
        })
        self.manager = ProviderRetentionManager(self.path, policy)

    def tearDown(self):
        self.temp.cleanup()

    def register(self, **overrides):
        values = dict(
            resource_id="resource-1", provider_id="image", tenant_id="tenant-a",
            resource_ref="remote-asset-1", data_categories=["user_content"],
            retention_days=2, now=1000,
        )
        values.update(overrides)
        return self.manager.register(**values)

    def test_registration_persists_only_opaque_reference_and_policy_metadata(self):
        result = self.register()
        self.assertTrue(result["saved"])
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("payload", raw)
        self.assertNotIn("credential", raw)
        restored = ProviderRetentionManager(self.path, self.manager.policy)
        self.assertEqual(restored.get_status("resource-1", tenant_id="tenant-a")["deletion_status"], "PENDING")

    def test_registration_retry_is_idempotent_and_does_not_extend_expiry(self):
        first = self.register()
        original_expiry = self.manager.get_status("resource-1", tenant_id="tenant-a")["expires_at"]
        retry = self.register(now=2000)
        self.assertTrue(first["saved"])
        self.assertEqual(retry["status"], "ALREADY_REGISTERED")
        self.assertEqual(
            self.manager.get_status("resource-1", tenant_id="tenant-a")["expires_at"],
            original_expiry,
        )

    def test_multiple_manager_instances_reload_shared_locked_state(self):
        other = ProviderRetentionManager(self.path, self.manager.policy)
        self.register()
        status = other.get_status("resource-1", tenant_id="tenant-a")
        self.assertEqual(status["deletion_status"], "PENDING")
        result = other.register(
            resource_id="resource-2", provider_id="image", tenant_id="tenant-b",
            resource_ref="remote-asset-2", data_categories=["public"],
            retention_days=1, now=1000,
        )
        self.assertTrue(result["saved"])
        self.assertEqual(
            self.manager.get_status("resource-2", tenant_id="tenant-b")["status"],
            "RETENTION_STATUS",
        )
        self.assertTrue(Path(str(self.path) + ".lock").exists())

    def test_policy_rejection_prevents_registration(self):
        result = self.register(data_categories=["credential"])
        self.assertFalse(result["saved"])
        self.assertFalse(self.path.exists())

    def test_not_due_resource_is_not_deleted(self):
        self.register(retention_days=2)
        calls = []
        result = self.manager.run_due(delete_provider_resource=lambda **kw: calls.append(kw) or True, now=1001)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(calls, [])

    def test_due_resource_deleted_and_idempotency_key_is_stable(self):
        self.register(retention_days=0)
        calls = []
        result = self.manager.run_due(delete_provider_resource=lambda **kw: calls.append(kw) or {"deleted": True}, now=1000)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(calls[0]["idempotency_key"], "retention-delete:resource-1")
        self.assertEqual(self.manager.get_status("resource-1", tenant_id="tenant-a")["deletion_status"], "DELETED")
        self.assertEqual(self.manager.run_due(delete_provider_resource=lambda **kw: self.fail("duplicate deletion"), now=1001)["deleted"], 0)

    def test_failed_deletion_is_retained_for_retry(self):
        self.register(retention_days=0)
        first = self.manager.run_due(delete_provider_resource=lambda **kw: False, now=1000)
        self.assertEqual(first["failed"], 1)
        second = self.manager.run_due(delete_provider_resource=lambda **kw: True, now=1001)
        self.assertEqual(second["deleted"], 1)
        self.assertEqual(self.manager.get_status("resource-1", tenant_id="tenant-a")["deletion_attempts"], 2)

    def test_tenant_scope_hides_other_tenant_resource(self):
        self.register()
        self.assertEqual(self.manager.get_status("resource-1", tenant_id="tenant-b")["status"], "RESOURCE_NOT_FOUND")

    def test_duplicate_id_conflict_does_not_overwrite(self):
        self.register()
        result = self.register(resource_ref="different-remote-asset")
        self.assertEqual(result["status"], "RESOURCE_ID_CONFLICT")
        self.assertEqual(self.manager.get_status("resource-1", tenant_id="tenant-a")["deletion_status"], "PENDING")

    def test_malformed_persisted_state_fails_closed(self):
        self.path.write_text(json.dumps({"version": 1, "resources": {"bad id": {}}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderRetentionManager(self.path, self.manager.policy)

    def test_invalid_limit_and_callback_fail_closed(self):
        self.assertEqual(self.manager.run_due(delete_provider_resource=None)["status"], "INVALID_DELETION_CALLBACK")
        self.assertEqual(self.manager.run_due(delete_provider_resource=lambda **kw: True, limit=True)["status"], "INVALID_LIMIT")


if __name__ == "__main__":
    unittest.main()
