"""Queue capacity, idempotency, async lifecycle, and backpressure tests."""
import json
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

    def test_trace_id_is_persisted_and_conflicts_are_rejected(self):
        first = self.manager.submit(
            job_id="trace-job", project_id="film", provider_id="image",
            payload_ref="payload:trace-job", trace_id="trace-123",
        )
        duplicate = self.manager.submit(
            job_id="trace-job", project_id="film", provider_id="image",
            payload_ref="payload:trace-job", trace_id="trace-123",
        )
        conflict = self.manager.submit(
            job_id="trace-job", project_id="film", provider_id="image",
            payload_ref="payload:trace-job", trace_id="trace-456",
        )
        self.assertEqual(first["job"]["trace_id"], "trace-123")
        self.assertEqual(duplicate["status"], "ALREADY_SUBMITTED")
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")
        self.assertEqual(
            self.manager.submit(
                job_id="bad-trace", project_id="film", provider_id="image",
                payload_ref="payload:bad-trace", trace_id="bad trace",
            )["status"],
            "INVALID_TRACE_ID",
        )

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

    def test_queued_job_past_deadline_fails_without_being_claimed(self):
        deadline = (self.now[0] + timedelta(seconds=5)).isoformat()
        result = self.manager.submit(
            job_id="job-deadline-queued", project_id="film", provider_id="image",
            payload_ref="payload:deadline-queued", deadline_at=deadline,
        )
        self.assertTrue(result["accepted"])
        self.now[0] += timedelta(seconds=6)
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "QUEUE_EMPTY")
        job = self.manager.poll(job_id="job-deadline-queued")["job"]
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error_code"], "DEADLINE_EXCEEDED")

    def test_running_job_cannot_report_success_after_deadline(self):
        deadline = (self.now[0] + timedelta(seconds=5)).isoformat()
        self.manager.submit(
            job_id="job-deadline-running", project_id="film", provider_id="image",
            payload_ref="payload:deadline-running", deadline_at=deadline,
        )
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.now[0] += timedelta(seconds=6)
        completed = self.manager.complete(
            job_id="job-deadline-running", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:too-late",
        )
        self.assertEqual(completed["job"]["status"], "FAILED")
        self.assertEqual(completed["job"]["error_code"], "DEADLINE_EXCEEDED")
        self.assertNotIn("result_ref", completed["job"])

    def test_job_deadline_requires_aware_future_timestamp(self):
        invalid = self.manager.submit(
            job_id="job-deadline-invalid", project_id="film", provider_id="image",
            payload_ref="payload:deadline-invalid", deadline_at="2026-10-16T12:05:00",
        )
        self.assertEqual(invalid["status"], "INVALID_DEADLINE")
        expired = self.manager.submit(
            job_id="job-deadline-past", project_id="film", provider_id="image",
            payload_ref="payload:deadline-past",
            deadline_at=(self.now[0] - timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(expired["status"], "DEADLINE_ALREADY_EXPIRED")

    def test_graceful_shutdown_drains_without_claiming_or_accepting_new_jobs(self):
        self.submit("job-before-shutdown")
        result = self.manager.request_shutdown(drain=True)
        self.assertEqual(result["status"], "SHUTDOWN_DRAINING")
        rejected = self.submit("job-during-shutdown")
        self.assertEqual(rejected["status"], "SHUTDOWN_IN_PROGRESS")
        blocked = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(blocked["status"], "SHUTDOWN_DRAINING")
        self.assertEqual(
            self.manager.poll(job_id="job-before-shutdown")["job"]["status"],
            "QUEUED",
        )
        self.assertEqual(self.manager.resume_work()["status"], "WORK_RESUMED")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["job"]["job_id"], "job-before-shutdown")

    def test_immediate_shutdown_cancels_queue_and_requests_running_cancel(self):
        self.submit("job-running", priority=90)
        self.submit("job-queued", priority=10)
        claimed = self.manager.claim_next(worker_id="worker-1")
        result = self.manager.request_shutdown(drain=False)
        self.assertEqual(result["status"], "SHUTDOWN_CANCELLING")
        self.assertTrue(self.manager.poll(job_id="job-queued")["job"]["status"] == "CANCELLED")
        running = self.manager.poll(job_id="job-running")["job"]
        self.assertTrue(running["cancel_requested"])
        finished = self.manager.complete(
            job_id="job-running", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:cancelled",
        )
        self.assertEqual(finished["job"]["status"], "CANCELLED")

    def test_shutdown_deadline_requests_cancellation_when_drain_expires(self):
        self.submit("job-running-deadline", priority=90)
        self.submit("job-queued-deadline", priority=10)
        claimed = self.manager.claim_next(worker_id="worker-1")
        deadline = (self.now[0] + timedelta(seconds=5)).isoformat()
        self.assertEqual(
            self.manager.request_shutdown(drain=True, deadline_at=deadline)["status"],
            "SHUTDOWN_DRAINING",
        )
        self.now[0] += timedelta(seconds=6)
        self.assertEqual(
            self.manager.claim_next(worker_id="worker-2")["status"],
            "SHUTDOWN_DRAINING",
        )
        self.assertEqual(
            self.manager.poll(job_id="job-queued-deadline")["job"]["status"],
            "CANCELLED",
        )
        self.assertTrue(
            self.manager.poll(job_id="job-running-deadline")["job"]["cancel_requested"]
        )
        finished = self.manager.complete(
            job_id="job-running-deadline", worker_id="worker-1", success=True,
            lease_token=claimed["job"]["lease_token"], result_ref="asset:late",
        )
        self.assertEqual(finished["job"]["status"], "CANCELLED")

    def test_state_persists_without_raw_payload_storage(self):
        self.submit("job-1")
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.poll(job_id="job-1")["job"]["status"], "QUEUED")
        state = self.path.read_text(encoding="utf-8")
        self.assertNotIn("actual prompt text", state)
        self.assertIn("payload:job-1", state)

    def test_malformed_persisted_trace_and_lease_fields_are_rejected(self):
        bad_path = self.path.with_name("bad-jobs.json")
        bad_path.write_text(json.dumps({
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1", "project_id": "film", "provider_id": "image",
                    "payload_ref": "payload:job-1", "priority": 50, "status": "QUEUED",
                    "attempts": 0, "trace_id": "invalid trace", "cancel_requested": False,
                }
            },
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderJobManager(bad_path)

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(self.manager.submit(
            job_id="bad id", project_id="film", provider_id="image",
            payload_ref="payload:bad",
        )["status"], "INVALID_JOB_ID")
        self.assertEqual(self.manager.claim_next(worker_id="bad worker")["status"], "INVALID_WORKER_ID")
        self.assertEqual(self.manager.poll(job_id="missing")["status"], "JOB_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
