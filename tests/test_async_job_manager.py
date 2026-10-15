import unittest
from datetime import datetime, timedelta, timezone

from projects.provider_store.async_job_manager import AsyncJobManager
from projects.provider_store.execution_queue import BoundedExecutionQueue


class AsyncJobManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.queue = BoundedExecutionQueue(max_pending=3, max_running=1, max_pending_per_tenant=2)
        self.jobs = AsyncJobManager(self.queue, max_job_records=10)

    def submit(self, request_id="job-1", **kwargs):
        values = {
            "request_id": request_id, "tenant_id": "tenant-a",
            "project_id": "project-a", "category": "image", "now": self.now,
        }
        values.update(kwargs)
        return self.jobs.submit(**values)

    def test_submit_poll_claim_complete_lifecycle(self):
        submitted = self.submit()
        self.assertEqual(submitted["status"], "QUEUED")
        queued = self.jobs.poll(request_id="job-1", tenant_id="tenant-a")
        self.assertEqual(queued["job"]["status"], "QUEUED")
        claimed = self.jobs.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(claimed["status"], "CLAIMED")
        self.assertEqual(claimed["job"]["status"], "RUNNING")
        completed = self.jobs.complete(
            request_id="job-1", worker_id="worker-1", status="SUCCEEDED",
            result_code="OUTPUT_READY", now=self.now + timedelta(seconds=1),
        )
        self.assertTrue(completed["completed"])
        result = self.jobs.poll(request_id="job-1", tenant_id="tenant-a")
        self.assertEqual(result["job"]["status"], "SUCCEEDED")
        self.assertEqual(result["job"]["result_code"], "OUTPUT_READY")
        self.assertIsNotNone(result["job"]["completed_at"])

    def test_polling_is_tenant_scoped_and_does_not_expose_worker_or_payload(self):
        self.submit()
        self.assertEqual(
            self.jobs.poll(request_id="job-1", tenant_id="tenant-b")["status"],
            "TENANT_SCOPE_MISMATCH",
        )
        rendered = repr(self.jobs.poll(request_id="job-1", tenant_id="tenant-a"))
        self.assertNotIn("worker_id", rendered)
        self.assertNotIn("fingerprint", rendered)

    def test_duplicate_submission_is_idempotent_and_conflicts_are_rejected(self):
        self.assertEqual(self.submit()["status"], "QUEUED")
        self.assertEqual(self.submit()["status"], "ALREADY_EXISTS")
        self.assertEqual(self.submit(project_id="other")["status"], "REQUEST_ID_CONFLICT")
        self.assertEqual(self.jobs.stats()["total_jobs"], 1)

    def test_wrong_worker_cannot_complete_job_and_malformed_result_code_is_rejected(self):
        self.submit()
        self.jobs.claim_next(worker_id="owner", now=self.now)
        self.assertEqual(self.jobs.complete(
            request_id="job-1", worker_id="attacker", status="SUCCEEDED", now=self.now
        )["status"], "WORKER_OWNERSHIP_MISMATCH")
        self.assertEqual(self.jobs.complete(
            request_id="job-1", worker_id="owner", status=[], now=self.now
        )["status"], "INVALID_TERMINAL_STATUS")
        self.assertEqual(self.jobs.complete(
            request_id="job-1", worker_id="owner", status="SUCCEEDED",
            result_code="TOKEN_SECRET_VALUE", now=self.now,
        )["status"], "INVALID_RESULT_CODE")
        self.assertEqual(self.jobs.poll(request_id="job-1", tenant_id="tenant-a")["job"]["status"], "RUNNING")

    def test_queued_cancellation_is_terminal_and_idempotent(self):
        self.submit()
        result = self.jobs.cancel(request_id="job-1", tenant_id="tenant-a", now=self.now)
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(self.jobs.poll(request_id="job-1", tenant_id="tenant-a")["job"]["status"], "CANCELLED")
        self.assertEqual(self.submit()["status"], "ALREADY_TERMINAL")

    def test_running_cancellation_does_not_free_capacity_until_worker_acknowledges(self):
        self.submit()
        self.jobs.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(
            self.jobs.cancel(request_id="job-1", tenant_id="tenant-a", now=self.now)["status"],
            "CANCEL_REQUESTED",
        )
        self.assertEqual(self.queue.stats()["running"], 1)
        self.assertEqual(self.jobs.complete(
            request_id="job-1", worker_id="worker-1", status="CANCELLED", now=self.now
        )["status"], "COMPLETED")
        self.assertEqual(self.queue.stats()["running"], 0)

    def test_expired_queued_job_and_running_deadline_are_handled_differently(self):
        deadline = self.now + timedelta(seconds=5)
        self.submit("queued-expired", priority=1, deadline_at=deadline.isoformat())
        self.submit("running-expired", priority=10, deadline_at=deadline.isoformat())
        self.jobs.claim_next(worker_id="worker", now=self.now)
        reaped = self.jobs.reap_expired(now=self.now + timedelta(seconds=6))
        self.assertEqual(reaped["expired"], 1)
        self.assertEqual(reaped["running_deadlines_flagged"], 1)
        self.assertEqual(self.jobs.poll(
            request_id="queued-expired", tenant_id="tenant-a"
        )["job"]["status"], "EXPIRED")
        running = self.jobs.poll(request_id="running-expired", tenant_id="tenant-a")["job"]
        self.assertTrue(running["deadline_exceeded"])
        self.assertEqual(running["status"], "RUNNING")
        self.assertEqual(self.queue.stats()["running"], 1)

    def test_queue_backpressure_and_job_ledger_capacity_fail_closed(self):
        queue = BoundedExecutionQueue(max_pending=1, max_running=1, max_pending_per_tenant=1)
        jobs = AsyncJobManager(queue, max_job_records=2)
        common = {"tenant_id": "tenant-a", "project_id": "project-a", "category": "image", "now": self.now}
        self.assertEqual(jobs.submit(request_id="a", **common)["status"], "QUEUED")
        self.assertEqual(jobs.submit(request_id="b", **common)["status"], "QUEUE_FULL")
        jobs.cancel(request_id="a", tenant_id="tenant-a", now=self.now)
        self.assertEqual(jobs.submit(request_id="b", **common)["status"], "QUEUED")
        self.assertEqual(jobs.submit(request_id="c", **common)["status"], "JOB_LEDGER_FULL")

    def test_invalid_inputs_and_safe_aggregate_stats(self):
        self.assertEqual(self.submit("bad\nrequest")["status"], "INVALID_JOB_METADATA")
        self.assertEqual(self.submit("bad-priority", priority=True)["status"], "INVALID_PRIORITY")
        self.assertEqual(self.jobs.poll(request_id="missing", tenant_id="tenant-a")["status"], "JOB_NOT_FOUND")
        stats = self.jobs.stats()
        self.assertEqual(stats["status"], "READY")
        self.assertNotIn("job-1", repr(stats))
        with self.assertRaises(ValueError):
            AsyncJobManager(self.queue, max_job_records=0)


if __name__ == "__main__":
    unittest.main()
