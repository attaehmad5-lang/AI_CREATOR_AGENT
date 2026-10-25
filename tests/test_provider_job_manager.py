"""Queue capacity, idempotency, async lifecycle, and backpressure tests."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_job_manager import ProviderJobManager


class ProviderJobManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "jobs.json"
        self.now = [datetime(2026, 10, 16, 12, tzinfo=timezone.utc)]
        self.manager = ProviderJobManager(
            self.path, max_queue_size=2, max_concurrent=1,
            lease_seconds=10, max_attempts=2,
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def submit(self, job_id, priority=50):
        return self.manager.submit(
            job_id=job_id, project_id="film", provider_id="image",
            payload_ref=f"payload:{job_id}", priority=priority,
        )

    def test_queue_capacity_applies_backpressure_and_returns_retry_hint(self):
        self.assertTrue(self.submit("job-1")["accepted"])
        self.assertTrue(self.submit("job-2")["accepted"])
        full = self.submit("job-3")
        self.assertEqual(full["status"], "QUEUE_FULL")
        self.assertEqual(full["retry_after_seconds"], 5)

    def test_submission_is_idempotent_and_conflicts_fail_closed(self):
        first = self.submit("job-1")
        same = self.submit("job-1")
        conflict = self.manager.submit(
            job_id="job-1", project_id="other", provider_id="image",
            payload_ref="payload:job-1", priority=50,
        )
        self.assertEqual(first["status"], "JOB_QUEUED")
        self.assertEqual(same["status"], "ALREADY_SUBMITTED")
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")

    def test_priority_order_and_concurrency_limit(self):
        self.submit("low", priority=10)
        self.submit("high", priority=90)
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["job"]["job_id"], "high")
        self.assertEqual(self.manager.claim_next(worker_id="worker-2")["status"], "CONCURRENCY_LIMIT")

    def test_priority_aging_prevents_old_low_priority_job_starvation(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=2,
            priority_aging_seconds=20, clock=lambda: self.now[0],
        )
        manager.submit(
            job_id="old-low", project_id="film", provider_id="image",
            payload_ref="payload:old", priority=10,
        )
        self.now[0] += timedelta(seconds=2000)
        manager.submit(
            job_id="new-high", project_id="film", provider_id="image",
            payload_ref="payload:new", priority=90,
        )
        claimed = manager.claim_next(worker_id="worker-aging")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["job_id"], "old-low")

    def test_job_lifecycle_poll_and_result_reference(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        done = self.manager.complete(
            job_id="job-1", worker_id="worker-1", success=True,
            result_ref="asset:result-1",
        )
        self.assertEqual(done["job"]["status"], "SUCCEEDED")
        self.assertEqual(self.manager.poll(job_id="job-1")["job"]["result_ref"], "asset:result-1")

    def test_wrong_worker_cannot_complete_job(self):
        self.submit("job-1")
        self.manager.claim_next(worker_id="worker-1")
        result = self.manager.complete(
            job_id="job-1", worker_id="worker-2", success=True,
            result_ref="asset:result-1",
        )
        self.assertEqual(result["status"], "JOB_NOT_OWNED_BY_WORKER")

    def test_cancel_queued_and_running_jobs_safely(self):
        self.submit("queued")
        self.assertEqual(self.manager.cancel(job_id="queued")["status"], "JOB_CANCELLED")
        self.submit("running")
        self.manager.claim_next(worker_id="worker-1")
        requested = self.manager.cancel(job_id="running")
        self.assertEqual(requested["status"], "CANCEL_REQUESTED")
        finished = self.manager.complete(
            job_id="running", worker_id="worker-1", success=True,
            result_ref="asset:unused",
        )
        self.assertEqual(finished["job"]["status"], "CANCELLED")

    def test_expired_worker_lease_requeues_then_fails_at_attempt_limit(self):
        self.submit("job-1")
        self.manager.claim_next(worker_id="worker-1")
        self.now[0] += timedelta(seconds=11)
        requeued = self.manager.claim_next(worker_id="worker-2")
        self.assertEqual(requeued["status"], "JOB_CLAIMED")
        self.assertEqual(requeued["job"]["attempts"], 2)
        self.now[0] += timedelta(seconds=11)
        exhausted = self.manager.claim_next(worker_id="worker-3")
        self.assertEqual(exhausted["status"], "QUEUE_EMPTY")
        self.assertEqual(self.manager.poll(job_id="job-1")["job"]["status"], "FAILED")

    def test_state_persists_without_raw_payload_storage(self):
        self.submit("job-1")
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.poll(job_id="job-1")["job"]["status"], "QUEUED")
        state = self.path.read_text(encoding="utf-8")
        self.assertNotIn("actual prompt text", state)
        self.assertIn("payload:job-1", state)

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(self.manager.submit(
            job_id="bad id", project_id="film", provider_id="image",
            payload_ref="payload:bad",
        )["status"], "INVALID_JOB_ID")
        self.assertEqual(self.manager.claim_next(worker_id="bad worker")["status"], "INVALID_WORKER_ID")
        self.assertEqual(self.manager.poll(job_id="missing")["status"], "JOB_NOT_FOUND")


class TenantScopedProviderJobTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "tenant-jobs.json"
        self.now = [datetime(2026, 10, 25, 12, tzinfo=timezone.utc)]
        self.manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=4, lease_seconds=10
        )

    def submit(self, job_id, tenant_id):
        return self.manager.submit(
            job_id=job_id,
            project_id="film",
            provider_id="image",
            payload_ref=f"payload:{job_id}",
            tenant_id=tenant_id,
        )

    def test_tenant_scoped_jobs_require_identity_for_poll_and_cancel(self):
        self.assertEqual(self.submit("tenant-job", "tenant-a")["status"], "JOB_QUEUED")
        self.assertEqual(self.manager.poll(job_id="tenant-job")["status"], "TENANT_ID_REQUIRED")
        self.assertEqual(
            self.manager.poll(job_id="tenant-job", tenant_id="tenant-b")["status"],
            "JOB_NOT_FOUND",
        )
        self.assertEqual(
            self.manager.cancel(job_id="tenant-job", tenant_id="tenant-b")["status"],
            "JOB_NOT_FOUND",
        )
        self.assertEqual(
            self.manager.cancel(job_id="tenant-job")["status"], "TENANT_ID_REQUIRED"
        )
        self.assertEqual(
            self.manager.poll(job_id="tenant-job", tenant_id="tenant-a")["status"],
            "JOB_STATUS",
        )

    def test_workers_only_claim_and_complete_jobs_for_their_tenant(self):
        self.submit("tenant-job", "tenant-a")
        self.assertEqual(
            self.manager.claim_next(worker_id="worker-1")["status"],
            "TENANT_ID_REQUIRED",
        )
        self.assertEqual(
            self.manager.claim_next(worker_id="worker-2", tenant_id="tenant-b")["status"],
            "QUEUE_EMPTY",
        )
        claimed = self.manager.claim_next(worker_id="worker-1", tenant_id="tenant-a")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["tenant_id"], "tenant-a")
        self.assertEqual(
            self.manager.complete(
                job_id="tenant-job", worker_id="worker-1", success=True,
                result_ref="asset:result",
            )["status"],
            "TENANT_ID_REQUIRED",
        )
        self.assertEqual(
            self.manager.complete(
                job_id="tenant-job", worker_id="worker-1", success=True,
                result_ref="asset:result", tenant_id="tenant-b",
            )["status"],
            "JOB_NOT_FOUND",
        )
        done = self.manager.complete(
            job_id="tenant-job", worker_id="worker-1", success=True,
            result_ref="asset:result", tenant_id="tenant-a",
        )
        self.assertEqual(done["status"], "JOB_COMPLETED")
        self.assertEqual(
            self.manager.poll(job_id="tenant-job", tenant_id="tenant-b")["status"],
            "JOB_NOT_FOUND",
        )

    def test_unscoped_worker_can_claim_legacy_jobs_but_not_tenant_jobs(self):
        self.submit("tenant-job", "tenant-a")
        legacy = self.manager.submit(
            job_id="legacy-job", project_id="film", provider_id="image",
            payload_ref="payload:legacy",
        )
        self.assertEqual(legacy["status"], "JOB_QUEUED")
        claimed = self.manager.claim_next(worker_id="legacy-worker")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["job_id"], "legacy-job")
        self.assertNotIn("tenant_id", claimed["job"])
        tenant_claimed = self.manager.claim_next(
            worker_id="tenant-worker", tenant_id="tenant-a"
        )
        self.assertEqual(tenant_claimed["status"], "JOB_CLAIMED")
        self.assertEqual(tenant_claimed["job"]["job_id"], "tenant-job")

    def test_job_id_cannot_be_reused_by_another_tenant(self):
        self.assertEqual(self.submit("shared-job", "tenant-a")["status"], "JOB_QUEUED")
        result = self.submit("shared-job", "tenant-b")
        self.assertEqual(result["status"], "REQUEST_ID_CONFLICT")
        self.assertFalse(result.get("accepted", True))
        self.assertNotIn("job", result)

    def test_cross_tenant_conflict_does_not_disclose_payload_reference(self):
        self.submit("private-job", "tenant-a")
        result = self.submit("private-job", "tenant-b")
        self.assertEqual(result["status"], "REQUEST_ID_CONFLICT")
        self.assertNotIn("job", result)
        self.assertNotIn("payload_ref", result)

    def test_per_tenant_queue_and_concurrency_limits_prevent_noisy_neighbor(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=4,
            max_queued_per_tenant=2, max_concurrent_per_tenant=1,
            clock=lambda: self.now[0],
        )
        def submit(job_id, tenant_id):
            return manager.submit(
                job_id=job_id, project_id="film", provider_id="image",
                payload_ref=f"payload:{job_id}", tenant_id=tenant_id,
            )
        self.assertEqual(submit("a1", "tenant-a")["status"], "JOB_QUEUED")
        self.assertEqual(submit("a2", "tenant-a")["status"], "JOB_QUEUED")
        self.assertEqual(submit("a3", "tenant-a")["status"], "TENANT_QUEUE_FULL")
        self.assertEqual(submit("b1", "tenant-b")["status"], "JOB_QUEUED")
        self.assertEqual(
            manager.claim_next(worker_id="wa1", tenant_id="tenant-a")["status"],
            "JOB_CLAIMED",
        )
        self.assertEqual(
            manager.claim_next(worker_id="wa2", tenant_id="tenant-a")["status"],
            "TENANT_CONCURRENCY_LIMIT",
        )
        other_tenant = manager.claim_next(worker_id="wb1", tenant_id="tenant-b")
        self.assertEqual(other_tenant["status"], "JOB_CLAIMED")
        self.assertEqual(other_tenant["job"]["tenant_id"], "tenant-b")

    def test_invalid_tenant_identifier_is_rejected(self):
        result = self.manager.submit(
            job_id="bad-tenant", project_id="film", provider_id="image",
            payload_ref="payload:bad", tenant_id="tenant with spaces",
        )
        self.assertEqual(result["status"], "INVALID_TENANT_ID")


if __name__ == "__main__":
    unittest.main()
