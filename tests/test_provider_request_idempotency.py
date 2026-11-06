import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_request_idempotency import ProviderRequestIdempotencyManager


class ProviderRequestIdempotencyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "request-idempotency.json"
        self.now = 1000.0
        self.manager = ProviderRequestIdempotencyManager(
            self.path, lease_seconds=10, max_records=100,
            clock=lambda: self.now,
        )
        self.fingerprint = "a" * 64

    def tearDown(self):
        self.temp.cleanup()

    def reserve(self, *, request_id="request-1", tenant_id="tenant-a", fingerprint=None):
        return self.manager.reserve_request(
            project_id="film", request_id=request_id, tenant_id=tenant_id,
            fingerprint=fingerprint or self.fingerprint,
        )

    def test_request_reservation_start_completion_and_duplicate_replay(self):
        reserved = self.reserve()
        self.assertTrue(reserved["reserved"])
        started = self.manager.mark_started(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=reserved["lease_token"],
        )
        self.assertTrue(started["saved"])
        completed = self.manager.finalize(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=reserved["lease_token"], outcome="completed",
            result_status="EXECUTION_SUCCEEDED",
        )
        self.assertTrue(completed["saved"])
        repeated_finalize = self.manager.finalize(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=reserved["lease_token"], outcome="completed",
            result_status="EXECUTION_SUCCEEDED",
        )
        self.assertEqual(repeated_finalize["status"], "ALREADY_FINALIZED")
        duplicate = self.reserve()
        self.assertEqual(duplicate["status"], "DUPLICATE_REQUEST")
        self.assertEqual(duplicate["result_status"], "EXECUTION_SUCCEEDED")

    def test_same_id_with_different_request_fingerprint_conflicts(self):
        self.reserve()
        conflict = self.reserve(fingerprint="b" * 64)
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")

    def test_concurrent_duplicate_reservation_is_blocked(self):
        first = self.reserve()
        second = self.reserve()
        self.assertTrue(first["reserved"])
        self.assertEqual(second["status"], "REQUEST_IN_PROGRESS")

    def test_expired_unstarted_lease_can_be_reclaimed_but_old_token_is_rejected(self):
        first = self.reserve()
        self.now += 11
        second = self.reserve()
        self.assertTrue(second["reserved"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        stale = self.manager.mark_started(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=first["lease_token"],
        )
        self.assertEqual(stale["status"], "IDEMPOTENCY_LEASE_MISMATCH")
        self.assertTrue(self.manager.mark_started(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=second["lease_token"],
        )["saved"])

    def test_not_started_state_allows_same_request_id_to_retry(self):
        first = self.reserve()
        not_started = self.manager.finalize(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=first["lease_token"], outcome="not_started",
            result_status="EXECUTION_NOT_STARTED",
        )
        self.assertTrue(not_started["saved"])
        retry = self.reserve()
        self.assertTrue(retry["reserved"])
        self.assertEqual(retry["attempt"], 2)

    def test_started_or_unknown_state_is_never_automatically_reclaimed(self):
        first = self.reserve()
        self.manager.mark_started(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=first["lease_token"],
        )
        self.now += 100000
        started = self.reserve()
        self.assertEqual(started["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.manager.finalize(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=first["lease_token"], outcome="unknown",
            result_status="EXECUTION_OUTCOME_UNKNOWN",
        )
        unknown = self.reserve()
        self.assertEqual(unknown["status"], "EXECUTION_OUTCOME_UNKNOWN")

    def test_tenant_scoping_allows_same_request_id_in_separate_tenants(self):
        first = self.reserve(tenant_id="tenant-a")
        second = self.reserve(tenant_id="tenant-b")
        self.assertTrue(first["reserved"])
        self.assertTrue(second["reserved"])
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(
            self.manager.get_status(
                project_id="film", request_id="request-1", tenant_id="tenant-a"
            )["state"],
            "RESERVED",
        )

    def test_multiple_instances_share_state_and_concurrent_reservation(self):
        other = ProviderRequestIdempotencyManager(
            self.path, lease_seconds=10, max_records=100,
            clock=lambda: self.now,
        )
        managers = [self.manager, other]

        def reserve(index):
            return managers[index % 2].reserve_request(
                project_id="film", request_id="race-request", tenant_id="tenant-a",
                fingerprint=self.fingerprint,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(result.get("reserved") is True for result in results), 1)
        self.assertEqual(sum(result.get("status") == "REQUEST_IN_PROGRESS" for result in results), 15)

    def test_malformed_persisted_state_fails_closed(self):
        self.path.write_text("{not-json", encoding="utf-8")
        result = self.reserve()
        self.assertEqual(result["status"], "IDEMPOTENCY_STATE_INVALID")

    def test_timestamp_inputs_must_be_numeric_and_monotonic(self):
        self.assertEqual(
            self.manager.reserve_request(
                project_id="film", request_id="bad-time",
                fingerprint=self.fingerprint, now="1000",
            )["status"],
            "INVALID_IDEMPOTENCY_TIMESTAMP",
        )
        reserved = self.reserve()
        backwards = self.manager.mark_started(
            project_id="film", request_id="request-1", tenant_id="tenant-a",
            lease_token=reserved["lease_token"], now=999,
        )
        self.assertEqual(backwards["status"], "INVALID_IDEMPOTENCY_TIMESTAMP")

    def test_invalid_identity_fingerprint_and_capacity_are_rejected(self):
        self.assertEqual(
            self.manager.reserve_request(
                project_id="bad project", request_id="request-1",
                fingerprint=self.fingerprint,
            )["status"],
            "INVALID_REQUEST_IDENTITY",
        )
        self.assertEqual(self.reserve(fingerprint="not-a-hash")["status"], "INVALID_REQUEST_FINGERPRINT")
        small_path = self.path.parent / "small.json"
        small = ProviderRequestIdempotencyManager(small_path, max_records=1)
        first = small.reserve_request(
            project_id="film", request_id="one", fingerprint=self.fingerprint,
        )
        self.assertTrue(first["reserved"])
        full = small.reserve_request(
            project_id="film", request_id="two", fingerprint=self.fingerprint,
        )
        self.assertEqual(full["status"], "IDEMPOTENCY_CAPACITY_REACHED")


if __name__ == "__main__":
    unittest.main()
