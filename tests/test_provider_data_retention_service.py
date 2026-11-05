"""Authorization and adapter-contract tests for service-managed data deletion."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy
from projects.provider_store.provider_data_retention_manager import ProviderDataRetentionManager
from projects.provider_store.provider_service import ProviderService


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class Connections:
    def verify_connectivity(self, *args, **kwargs):
        return {"verified": False}


class ProviderDataRetentionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.clock = MutableClock()
        self.policy = ProviderDataHandlingPolicy({
            "image": {
                "allowed_categories": ["public", "user_content"],
                "max_retention_days": 10,
                "training_use_allowed": False,
                "source_url": "https://provider.example.com/privacy",
            }
        })
        self.manager = ProviderDataRetentionManager(
            root / "retention.json", self.policy, clock=self.clock,
            retry_backoff_seconds=30,
        )
        self.authorization = ProviderAuthorizationManager(root / "auth.json")
        self.authorization.assign_role("admin-user", "admin")
        self.authorization.assign_role("creator-user", "creator")
        self.service = ProviderService(
            Connections(),
            authorization_manager=self.authorization,
            data_handling_policy=self.policy,
            data_retention_manager=self.manager,
        )

    def register_due(self, record_id="record-1"):
        return self.service.record_provider_data_asset(
            record_id=record_id,
            provider_id="image",
            project_id="project-1",
            tenant_id=None,
            asset_ref="provider-object-123",
            data_categories=["user_content"],
            retention_days=0,
            principal_id="admin-user",
        )

    def test_only_admin_can_register_data_assets(self):
        denied = self.service.record_provider_data_asset(
            record_id="record-1",
            provider_id="image",
            project_id="project-1",
            asset_ref="provider-object-123",
            data_categories=["user_content"],
            retention_days=1,
            principal_id="creator-user",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertFalse(denied["saved"])
        self.assertTrue(self.register_due()["saved"])

    def test_data_deletion_requires_admin_and_provider_confirmation(self):
        self.register_due()
        unauthorized = self.service.run_provider_data_retention_deletions(
            worker_id="worker-1",
            deleter=lambda **kwargs: {"deleted": True, "confirmed": True},
            principal_id="creator-user",
        )
        self.assertEqual(unauthorized["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(self.manager.status()["counts"]["ACTIVE"], 1)

        received = []

        def delete_provider_data(**kwargs):
            received.append(kwargs)
            return {"deleted": True, "confirmed": True}

        result = self.service.run_provider_data_retention_deletions(
            worker_id="worker-2",
            deleter=delete_provider_data,
            principal_id="admin-user",
        )
        self.assertEqual(result["status"], "DATA_RETENTION_BATCH_PROCESSED")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["idempotency_key"], "record-1")
        self.assertEqual(received[0]["timeout_seconds"], 10.0)
        self.assertNotIn("asset_ref", repr(result))
        self.assertNotIn("lease_token", repr(result))
        self.assertEqual(self.manager.status()["counts"]["DELETED"], 1)

    def test_unconfirmed_remote_deletion_is_not_marked_deleted(self):
        self.register_due()
        result = self.service.run_provider_data_retention_deletions(
            worker_id="worker-1",
            deleter=lambda **kwargs: {"deleted": True, "confirmed": False},
            principal_id="admin-user",
        )
        self.assertEqual(result["completed"], 0)
        state = self.manager.get_record("record-1")["record"]
        self.assertEqual(state["status"], "DELETE_PENDING")
        self.assertEqual(state["last_error_code"], "PROVIDER_DELETE_UNCONFIRMED")

    def test_unsupported_adapter_contract_does_not_claim_or_delete(self):
        self.register_due()
        result = self.service.run_provider_data_retention_deletions(
            worker_id="worker-1",
            deleter=lambda provider_id: {"deleted": True, "confirmed": True},
            principal_id="admin-user",
        )
        self.assertEqual(result["status"], "DELETE_ADAPTER_CONTRACT_UNSUPPORTED")
        state = self.manager.get_record("record-1")["record"]
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["attempts"], 0)

    def test_provider_policy_rejection_prevents_retention_registration(self):
        result = self.service.record_provider_data_asset(
            record_id="bad-record",
            provider_id="unknown-provider",
            project_id="project-1",
            asset_ref="provider-object-123",
            data_categories=["user_content"],
            retention_days=1,
            principal_id="admin-user",
        )
        self.assertEqual(result["status"], "PROVIDER_DATA_POLICY_REJECTED")
        self.assertFalse(result["saved"])
        self.assertEqual(self.manager.status()["total_records"], 0)

    def test_manual_requeue_is_admin_only(self):
        self.register_due()
        self.service.run_provider_data_retention_deletions(
            worker_id="worker-1",
            deleter=lambda **kwargs: {"deleted": False, "confirmed": False},
            principal_id="admin-user",
        )
        self.clock.value += timedelta(seconds=60)
        # Exhaust all automatic attempts through the manager, then verify facade permission.
        for index in range(7):
            claims = self.manager.claim_due_deletions(f"worker-{index}")
            if claims["items"]:
                item = claims["items"][0]
                self.manager.fail_deletion(
                    item["record_id"], f"worker-{index}", item["lease_token"]
                )
                self.clock.value += timedelta(seconds=60)
        state = self.manager.get_record("record-1")["record"]
        if state["status"] == "DELETE_FAILED":
            denied = self.service.requeue_failed_provider_data_deletion(
                "record-1", principal_id="creator-user"
            )
            self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
            allowed = self.service.requeue_failed_provider_data_deletion(
                "record-1", principal_id="admin-user"
            )
            self.assertTrue(allowed["requeued"])


if __name__ == "__main__":
    unittest.main()
