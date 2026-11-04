"""Regression tests for multi-store privacy deletion and retention orchestration."""
import unittest
from datetime import datetime, timezone

from projects.provider_store.privacy_data_lifecycle import PrivacyDataLifecycleManager


class StoreStub:
    def __init__(self, *, delete=None, purge=None):
        self.delete_result = delete or {
            "confirmed": True, "status": "DELETED", "deleted_count": 2,
        }
        self.purge_result = purge or {
            "confirmed": True, "status": "PURGED", "deleted_count": 3,
        }
        self.calls = []

    def delete_subject_data(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return self.delete_result

    def purge_expired_data(self, **kwargs):
        self.calls.append(("purge", kwargs))
        return self.purge_result


class PrivacyDataLifecycleManagerTests(unittest.TestCase):
    def test_registration_rejects_invalid_duplicate_and_unsupported_stores(self):
        manager = PrivacyDataLifecycleManager(max_stores=1)
        store = StoreStub()
        self.assertTrue(manager.register_store("primary", store)["registered"])
        with self.assertRaises(ValueError):
            manager.register_store("primary", StoreStub())
        with self.assertRaises(ValueError):
            manager.register_store("../outside", StoreStub())
        with self.assertRaises(ValueError):
            manager.register_store("second", StoreStub())
        with self.assertRaises(ValueError):
            PrivacyDataLifecycleManager({"bad": object()})

    def test_subject_deletion_requires_confirmation_and_uses_scoped_request(self):
        store = StoreStub()
        manager = PrivacyDataLifecycleManager({"database": store})
        pending = manager.delete_subject_data(
            subject_ref="subject-17", project_id="film", tenant_id="tenant-1",
            request_id="delete-001",
        )
        self.assertEqual(pending["status"], "SUBJECT_DELETION_CONFIRMATION_REQUIRED")
        self.assertEqual(store.calls, [])

        result = manager.delete_subject_data(
            subject_ref="subject-17", project_id="film", tenant_id="tenant-1",
            request_id="delete-001", confirmed=True,
        )
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETED")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 2)
        self.assertEqual(store.calls[0][1], {
            "subject_ref": "subject-17", "project_id": "film",
            "tenant_id": "tenant-1", "request_id": "delete-001",
        })
        self.assertNotIn("subject-17", repr(result))

    def test_deletion_continues_after_one_store_failure_and_reports_partial(self):
        failed = StoreStub(delete={
            "confirmed": False, "status": "PENDING", "deleted_count": 0,
        })
        successful = StoreStub()
        manager = PrivacyDataLifecycleManager({
            "database": failed, "object-store": successful,
        })
        result = manager.delete_subject_data(
            subject_ref="subject-18", project_id="film", tenant_id=None,
            request_id="delete-002", confirmed=True,
        )
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETION_PARTIAL")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["deleted_count"], 2)
        self.assertFalse(result["stores"]["database"]["confirmed"])
        self.assertTrue(result["stores"]["object-store"]["confirmed"])
        self.assertEqual(len(successful.calls), 1)

    def test_invalid_adapter_output_fails_closed_without_echoing_payload(self):
        marker = "private-payload-marker"
        manager = PrivacyDataLifecycleManager({
            "database": StoreStub(delete={
                "confirmed": True, "status": "UNTRUSTED_" + marker,
                "deleted_count": 2, "payload": marker,
            }),
        })
        result = manager.delete_subject_data(
            subject_ref="subject-19", project_id="film", tenant_id=None,
            request_id="delete-003", confirmed=True,
        )
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETION_PARTIAL")
        self.assertEqual(result["stores"]["database"]["status"], "INVALID_ADAPTER_RESPONSE")
        self.assertNotIn(marker, repr(result))

    def test_adapter_exception_is_redacted_and_other_stores_still_run(self):
        class BrokenStore(StoreStub):
            def delete_subject_data(self, **kwargs):
                raise RuntimeError("private value must not escape")

        working = StoreStub()
        manager = PrivacyDataLifecycleManager({
            "broken": BrokenStore(), "working": working,
        })
        result = manager.delete_subject_data(
            subject_ref="subject-20", project_id="film", tenant_id=None,
            request_id="delete-004", confirmed=True,
        )
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETION_PARTIAL")
        self.assertEqual(result["stores"]["broken"]["status"], "ADAPTER_OPERATION_FAILED")
        self.assertEqual(result["stores"]["broken"]["error_type"], "RuntimeError")
        self.assertNotIn("private value", repr(result))
        self.assertEqual(len(working.calls), 1)

    def test_email_subject_reference_is_rejected(self):
        manager = PrivacyDataLifecycleManager({"database": StoreStub()})
        result = manager.delete_subject_data(
            subject_ref="person@example.com", project_id="film", tenant_id=None,
            request_id="delete-005", confirmed=True,
        )
        self.assertEqual(result["status"], "INVALID_SUBJECT_DELETION_SCOPE")

    def test_retention_purge_requires_aware_cutoff_and_confirms_every_store(self):
        store = StoreStub()
        manager = PrivacyDataLifecycleManager({"database": store})
        naive = manager.purge_expired_data(
            cutoff=datetime(2026, 1, 1), project_id="film", tenant_id=None,
            request_id="purge-001", confirmed=True,
        )
        self.assertEqual(naive["status"], "INVALID_RETENTION_PURGE_SCOPE")
        self.assertEqual(store.calls, [])

        result = manager.purge_expired_data(
            cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
            project_id="film", tenant_id="tenant-1", request_id="purge-001",
            confirmed=True,
        )
        self.assertEqual(result["status"], "RETENTION_PURGE_CONFIRMED")
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 3)
        self.assertEqual(store.calls[0][0], "purge")

    def test_missing_store_configuration_never_reports_success(self):
        manager = PrivacyDataLifecycleManager()
        result = manager.delete_subject_data(
            subject_ref="subject-21", project_id="film", tenant_id=None,
            request_id="delete-006", confirmed=True,
        )
        self.assertEqual(result["status"], "PRIVACY_STORES_NOT_CONFIGURED")
        self.assertFalse(result["completed"])


if __name__ == "__main__":
    unittest.main()
