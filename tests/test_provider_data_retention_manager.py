"""Cross-process and lease-safety tests for provider data retention."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy
from projects.provider_store.provider_data_retention_manager import ProviderDataRetentionManager


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class ProviderDataRetentionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = MutableClock()
        self.policy = ProviderDataHandlingPolicy({
            "image": {
                "allowed_categories": ["public", "user_content", "personal"],
                "max_retention_days": 30,
                "training_use_allowed": False,
                "source_url": "https://provider.example.com/privacy",
            }
        })
        self.manager = ProviderDataRetentionManager(
            self.root / "retention.json",
            self.policy,
            clock=self.clock,
            max_attempts=2,
            retry_backoff_seconds=30,
        )

    def register(self, *, record_id="asset-1", retention_days=1, tenant_id="tenant-1"):
        return self.manager.register_asset(
            record_id,
            provider_id="image",
            project_id="project-1",
            tenant_id=tenant_id,
            asset_ref="provider-object-123",
            data_categories=["user_content"],
            retention_days=retention_days,
        )

    def test_registration_requires_provider_policy_and_is_idempotent(self):
        rejected = self.manager.register_asset(
            "asset-bad",
            provider_id="unknown",
            project_id="project-1",
            asset_ref="opaque-ref",
            data_categories=["user_content"],
            retention_days=1,
        )
        self.assertFalse(rejected["saved"])
        self.assertEqual(rejected["status"], "PROVIDER_DATA_POLICY_REJECTED")
        self.assertTrue(self.register()["saved"])
        duplicate = self.register()
        self.assertEqual(duplicate["status"], "ASSET_ALREADY_REGISTERED")
        conflict = self.manager.register_asset(
            "asset-1",
            provider_id="image",
            project_id="project-1",
            tenant_id="tenant-1",
            asset_ref="different-ref",
            data_categories=["user_content"],
            retention_days=1,
        )
        self.assertEqual(conflict["status"], "RECORD_ID_CONFLICT")

    def test_expiry_claim_and_completion_remove_resource_reference(self):
        self.assertTrue(self.register(retention_days=0)["saved"])
        claimed = self.manager.claim_due_deletions("worker-1")
        self.assertEqual(claimed["status"], "DELETION_CLAIMS_ISSUED")
        self.assertEqual(len(claimed["items"]), 1)
        item = claimed["items"][0]
        self.assertEqual(item["asset_ref"], "provider-object-123")
        done = self.manager.complete_deletion(
            item["record_id"], "worker-1", item["lease_token"]
        )
        self.assertTrue(done["completed"])
        record = self.manager.get_record("asset-1", tenant_id="tenant-1")["record"]
        self.assertEqual(record["status"], "DELETED")
        self.assertNotIn("asset_ref", record)
        persisted = json.loads((self.root / "retention.json").read_text(encoding="utf-8"))
        self.assertIsNone(persisted["records"]["asset-1"]["asset_ref"])
        self.assertEqual(persisted["records"]["asset-1"]["data_categories"], [])

    def test_wrong_worker_and_wrong_token_cannot_complete_deletion(self):
        self.register(retention_days=0)
        item = self.manager.claim_due_deletions("worker-1")["items"][0]
        wrong_worker = self.manager.complete_deletion(
            "asset-1", "worker-2", item["lease_token"]
        )
        self.assertEqual(wrong_worker["status"], "DELETION_LEASE_OWNER_MISMATCH")
        wrong_token = self.manager.complete_deletion(
            "asset-1", "worker-1", "wrong-token"
        )
        self.assertEqual(wrong_token["status"], "DELETION_LEASE_INVALID")
        correct = self.manager.complete_deletion(
            "asset-1", "worker-1", item["lease_token"]
        )
        self.assertTrue(correct["completed"])

    def test_failed_deletion_uses_bounded_retry_and_manual_requeue(self):
        self.register(retention_days=0)
        first = self.manager.claim_due_deletions("worker-1")["items"][0]
        failed_once = self.manager.fail_deletion(
            "asset-1", "worker-1", first["lease_token"],
            error_code="REMOTE_DELETE_UNAVAILABLE",
        )
        self.assertEqual(failed_once["status"], "DELETION_RETRY_SCHEDULED")
        self.assertEqual(
            self.manager.claim_due_deletions("worker-2")["items"], []
        )
        self.clock.advance(seconds=30)
        second = self.manager.claim_due_deletions("worker-2")["items"][0]
        failed_twice = self.manager.fail_deletion(
            "asset-1", "worker-2", second["lease_token"],
            error_code="API_TOKEN_LEAKED",
        )
        self.assertEqual(failed_twice["status"], "DELETION_RETRY_LIMIT_REACHED")
        record = self.manager.get_record("asset-1", tenant_id="tenant-1")["record"]
        self.assertEqual(record["status"], "DELETE_FAILED")
        self.assertEqual(record["last_error_code"], "PROVIDER_DELETE_FAILED")
        self.assertTrue(self.manager.requeue_failed("asset-1")["requeued"])
        third = self.manager.claim_due_deletions("worker-3")["items"][0]
        self.assertTrue(self.manager.complete_deletion(
            "asset-1", "worker-3", third["lease_token"]
        )["completed"])

    def test_expired_worker_lease_is_recovered_and_old_token_rejected(self):
        self.register(retention_days=0)
        old = self.manager.claim_due_deletions(
            "worker-old", lease_seconds=15
        )["items"][0]
        self.clock.advance(seconds=16)
        new = self.manager.claim_due_deletions("worker-new")["items"][0]
        stale = self.manager.complete_deletion(
            "asset-1", "worker-old", old["lease_token"]
        )
        self.assertEqual(stale["status"], "DELETION_LEASE_OWNER_MISMATCH")
        self.assertTrue(self.manager.complete_deletion(
            "asset-1", "worker-new", new["lease_token"]
        )["completed"])

    def test_tenant_scope_hides_other_tenant_records_and_asset_reference(self):
        self.register()
        hidden = self.manager.get_record("asset-1", tenant_id="tenant-2")
        self.assertEqual(hidden["status"], "RETENTION_RECORD_NOT_FOUND")
        visible = self.manager.get_record("asset-1", tenant_id="tenant-1")
        self.assertEqual(visible["status"], "RETENTION_RECORD_FOUND")
        self.assertNotIn("asset_ref", visible["record"])

    def test_parallel_managers_issue_one_lease_for_one_expired_asset(self):
        self.register(retention_days=0)
        managers = [
            ProviderDataRetentionManager(
                self.root / "retention.json", self.policy, clock=self.clock,
                max_attempts=2, retry_backoff_seconds=30,
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(2)

        def claim(index):
            barrier.wait()
            return managers[index].claim_due_deletions(f"worker-{index}")["items"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(sum(len(items) for items in results), 1)

    def test_malformed_persisted_state_fails_closed(self):
        self.register()
        path = self.root / "retention.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["records"]["asset-1"]["lease_token_digest"] = "plaintext-secret"
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderDataRetentionManager(path, self.policy, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
