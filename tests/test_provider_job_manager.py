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

    def test_provider_specific_concurrency_skips_blocked_provider_queue(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=3,
            max_concurrent_by_provider={"image": 1, "video": 2},
            clock=lambda: self.now[0],
        )
        manager.submit(job_id="image-high", project_id="film", provider_id="image",
                       payload_ref="payload:image-high", priority=90)
        manager.submit(job_id="image-low", project_id="film", provider_id="image",
                       payload_ref="payload:image-low", priority=80)
        manager.submit(job_id="video-mid", project_id="film", provider_id="video",
                       payload_ref="payload:video-mid", priority=70)
        first = manager.claim_next(worker_id="worker-1")
        second = manager.claim_next(worker_id="worker-2")
        third = manager.claim_next(worker_id="worker-3")
        self.assertEqual(first["job"]["job_id"], "image-high")
        self.assertEqual(second["job"]["job_id"], "video-mid")
        self.assertEqual(third["status"], "PROVIDER_CONCURRENCY_LIMIT")

    def test_invalid_provider_concurrency_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderJobManager(self.path, max_concurrent_by_provider={"bad provider": 1})
        with self.assertRaises(ValueError):
            ProviderJobManager(self.path, max_concurrent_by_provider={"image": True})

    def test_job_lifecycle_poll_and_result_reference(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        done = self.manager.complete(
            job_id="job-1", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:result-1",
        )
        self.assertEqual(done["job"]["status"], "SUCCEEDED")
        self.assertEqual(self.manager.poll(job_id="job-1")["job"]["result_ref"], "asset:result-1")

    def test_wrong_worker_cannot_complete_job(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        result = self.manager.complete(
            job_id="job-1", worker_id="worker-2", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:result-1",
        )
        self.assertEqual(result["status"], "JOB_NOT_OWNED_BY_WORKER")

    def test_cancel_queued_and_running_jobs_safely(self):
        self.submit("queued")
        self.assertEqual(self.manager.cancel(job_id="queued")["status"], "JOB_CANCELLED")
        self.submit("running")
        claimed = self.manager.claim_next(worker_id="worker-1")
        requested = self.manager.cancel(job_id="running")
        self.assertEqual(requested["status"], "CANCEL_REQUESTED")
        finished = self.manager.complete(
            job_id="running", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:unused",
        )
        self.assertEqual(finished["job"]["status"], "CANCELLED")

    def test_expired_worker_cannot_complete_job(self):
        self.submit("job-expired")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.now[0] += timedelta(seconds=11)
        result = self.manager.complete(
            job_id="job-expired", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:late",
        )
        self.assertEqual(result["status"], "JOB_LEASE_EXPIRED")
        job = self.manager.poll(job_id="job-expired")["job"]
        self.assertEqual(job["status"], "QUEUED")
        self.assertNotIn("result_ref", job)
        self.assertNotIn("lease_token", job)

    def test_stale_lease_token_cannot_complete_reclaimed_job(self):
        self.submit("job-fenced")
        first = self.manager.claim_next(worker_id="worker-1")
        self.now[0] += timedelta(seconds=11)
        second = self.manager.claim_next(worker_id="worker-1")
        stale = self.manager.complete(
            job_id="job-fenced", worker_id="worker-1", success=True,
            lease_token=first["job"]["lease_token"], result_ref="asset:stale",
        )
        self.assertEqual(stale["status"], "JOB_LEASE_TOKEN_MISMATCH")
        current = self.manager.complete(
            job_id="job-fenced", worker_id="worker-1", success=True,
            lease_token=second["job"]["lease_token"], result_ref="asset:current",
        )
        self.assertEqual(current["job"]["result_ref"], "asset:current")

    def test_job_completion_requires_lease_token(self):
        self.submit("job-no-token")
        self.manager.claim_next(worker_id="worker-1")
        result = self.manager.complete(
            job_id="job-no-token", worker_id="worker-1", success=True,
            result_ref="asset:result",
        )
        self.assertEqual(result["status"], "LEASE_TOKEN_REQUIRED")

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


if __name__ == "__main__":
    unittest.main()
