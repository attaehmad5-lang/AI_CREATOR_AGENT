"""Regression tests for multi-store privacy deletion and retention orchestration."""
import threading
import unittest
from datetime import datetime, timezone

from projects.provider_store.privacy_data_lifecycle import (
    DURABLE_PRIVACY_ADAPTER_CAPABILITY,
    PrivacyDataLifecycleManager,
)


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


class DurableStoreStub(StoreStub):
    _privacy_durable_idempotency_capability = DURABLE_PRIVACY_ADAPTER_CAPABILITY


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

    def test_strict_mode_requires_durable_idempotency_for_initial_and_late_stores(self):
        with self.assertRaisesRegex(ValueError, "durable idempotency"):
            PrivacyDataLifecycleManager(
                {"raw-store": StoreStub()}, require_durable_idempotency=True
            )

        manager = PrivacyDataLifecycleManager(
            {"wrapped-store": DurableStoreStub()}, require_durable_idempotency=True
        )
        self.assertTrue(manager.durable_idempotency_ready)
        with self.assertRaisesRegex(ValueError, "durable idempotency"):
            manager.register_store("late-raw-store", StoreStub())
        manager.register_store("late-wrapped-store", DurableStoreStub())
        self.assertTrue(manager.durable_idempotency_ready)

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

    def test_numeric_phone_like_subject_reference_is_rejected(self):
        manager = PrivacyDataLifecycleManager({"database": StoreStub()})
        result = manager.delete_subject_data(
            subject_ref="14155550134", project_id="film", tenant_id=None,
            request_id="delete-007", confirmed=True,
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

    def test_registration_during_operation_is_deferred_to_next_run(self):
        manager = PrivacyDataLifecycleManager()

        class RegisteringStore(StoreStub):
            def delete_subject_data(self, **kwargs):
                self.calls.append(("delete", kwargs))
                manager.register_store("late-store", late_store)
                return {"confirmed": True, "status": "DELETED", "deleted_count": 1}

        late_store = StoreStub()
        manager.register_store("initial-store", RegisteringStore())
        result = manager.delete_subject_data(
            subject_ref="subject-22", project_id="film", tenant_id=None,
            request_id="delete-008", confirmed=True,
        )
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETED")
        self.assertEqual(result["store_count"], 1)
        self.assertEqual(set(result["stores"]), {"initial-store"})
        self.assertEqual(late_store.calls, [])


    def test_concurrent_registration_is_deferred_to_next_run(self):
        manager = PrivacyDataLifecycleManager()
        late_store = StoreStub()
        registration_errors = []

        class ConcurrentRegisteringStore(StoreStub):
            def __init__(self):
                super().__init__()
                self.registration_started = False

            def delete_subject_data(self, **kwargs):
                self.calls.append(("delete", kwargs))
                if not self.registration_started:
                    self.registration_started = True

                    def register_late_store():
                        try:
                            manager.register_store("late-store", late_store)
                        except Exception as exc:
                            registration_errors.append(exc)

                    worker = threading.Thread(target=register_late_store)
                    worker.start()
                    worker.join(timeout=3)
                    if worker.is_alive():
                        raise RuntimeError("concurrent store registration did not finish")
                    if registration_errors:
                        raise registration_errors[0]
                return {"confirmed": True, "status": "DELETED", "deleted_count": 1}

        manager.register_store("initial-store", ConcurrentRegisteringStore())
        first = manager.delete_subject_data(
            subject_ref="subject-23", project_id="film", tenant_id=None,
            request_id="delete-009", confirmed=True,
        )
        self.assertEqual(first["status"], "SUBJECT_DATA_DELETED")
        self.assertEqual(first["store_count"], 1)
        self.assertEqual(set(first["stores"]), {"initial-store"})
        self.assertEqual(manager.registered_store_count, 2)
        self.assertEqual(late_store.calls, [])

        second = manager.delete_subject_data(
            subject_ref="subject-24", project_id="film", tenant_id=None,
            request_id="delete-010", confirmed=True,
        )
        self.assertEqual(second["status"], "SUBJECT_DATA_DELETED")
        self.assertEqual(second["store_count"], 2)
        self.assertEqual(len(late_store.calls), 1)

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
