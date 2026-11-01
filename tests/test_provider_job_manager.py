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

    def test_job_history_capacity_preserves_terminal_idempotency_records(self):
        limited = ProviderJobManager(
            self.path, max_queue_size=2, max_concurrent=1,
            max_job_records=1, clock=lambda: self.now[0],
        )
        first = limited.submit(
            job_id="job-1", project_id="film", provider_id="image",
            payload_ref="payload:job-1",
        )
        claimed = limited.claim_next(worker_id="worker-1")
        completed = limited.complete(
            job_id="job-1", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:job-1",
        )
        second = limited.submit(
            job_id="job-2", project_id="film", provider_id="image",
            payload_ref="payload:job-2",
        )
        duplicate = limited.submit(
            job_id="job-1", project_id="film", provider_id="image",
            payload_ref="payload:job-1",
        )
        self.assertEqual(first["status"], "JOB_QUEUED")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(completed["status"], "JOB_COMPLETED")
        self.assertEqual(second["status"], "JOB_HISTORY_CAPACITY_REACHED")
        self.assertEqual(duplicate["status"], "ALREADY_SUBMITTED")
        self.assertEqual(len(limited.data["jobs"]), 1)

    def test_job_store_rejects_unsupported_version(self):
        self.path.write_text('{"version":"2.0","jobs":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "job state schema"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

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

    def test_job_lifecycle_poll_and_result_reference(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        done = self.manager.complete(
            job_id="job-1", worker_id="worker-1", lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:result-1",
        )
        self.assertEqual(done["job"]["status"], "SUCCEEDED")
        self.assertEqual(self.manager.poll(job_id="job-1")["job"]["result_ref"], "asset:result-1")

    def test_worker_can_renew_current_lease_and_stale_token_cannot(self):
        self.submit("job-1")
        first = self.manager.claim_next(worker_id="worker-1")
        old_token = first["job"]["lease_token"]
        self.now[0] += timedelta(seconds=8)
        renewed = self.manager.renew_lease(
            job_id="job-1", worker_id="worker-1", lease_token=old_token
        )
        self.assertEqual(renewed["status"], "JOB_LEASE_RENEWED")
        self.assertTrue(renewed["renewed"])
        self.assertEqual(
            datetime.fromisoformat(renewed["lease_until"]),
            self.now[0] + timedelta(seconds=10),
        )

        self.now[0] += timedelta(seconds=11)
        next_claim = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(next_claim["status"], "JOB_CLAIMED")
        stale = self.manager.renew_lease(
            job_id="job-1", worker_id="worker-1", lease_token=old_token
        )
        self.assertEqual(stale["status"], "JOB_LEASE_NOT_OWNED")
        current = self.manager.renew_lease(
            job_id="job-1", worker_id="worker-1",
            lease_token=next_claim["job"]["lease_token"],
        )
        self.assertEqual(current["status"], "JOB_LEASE_RENEWED")

    def test_stale_lease_token_cannot_complete_reclaimed_job_even_for_same_worker(self):
        self.submit("job-1")
        first = self.manager.claim_next(worker_id="worker-1")
        old_token = first["job"]["lease_token"]
        self.now[0] += timedelta(seconds=11)
        second = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(second["status"], "JOB_CLAIMED")
        self.assertNotEqual(second["job"]["lease_token"], old_token)

        stale = self.manager.complete(
            job_id="job-1", worker_id="worker-1",
            lease_token=old_token, success=True,
            result_ref="asset:stale-result",
        )
        self.assertEqual(stale["status"], "JOB_LEASE_NOT_OWNED")
        self.assertEqual(
            self.manager.poll(job_id="job-1")["job"]["status"], "RUNNING"
        )
        current = self.manager.complete(
            job_id="job-1", worker_id="worker-1",
            lease_token=second["job"]["lease_token"], success=True,
            result_ref="asset:current-result",
        )
        self.assertEqual(current["status"], "JOB_COMPLETED")
        self.assertEqual(
            current["job"]["result_ref"], "asset:current-result"
        )

    def test_wrong_worker_cannot_complete_job(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        result = self.manager.complete(
            job_id="job-1", worker_id="worker-2", lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:result-1",
        )
        self.assertEqual(result["status"], "JOB_LEASE_NOT_OWNED")

    def test_cancel_queued_and_running_jobs_safely(self):
        self.submit("queued")
        self.assertEqual(self.manager.cancel(job_id="queued")["status"], "JOB_CANCELLED")
        self.submit("running")
        claimed = self.manager.claim_next(worker_id="worker-1")
        requested = self.manager.cancel(job_id="running")
        self.assertEqual(requested["status"], "CANCEL_REQUESTED")
        finished = self.manager.complete(
            job_id="running", worker_id="worker-1", lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:unused",
        )
        self.assertEqual(finished["job"]["status"], "CANCELLED")

    def test_malformed_or_missing_lease_deadlines_reap_running_jobs_safely(self):
        for index, bad_lease in enumerate(
            ("2026-10-16T12:00:00", None, "not-a-timestamp")
        ):
            path = Path(self.temp_dir.name) / f"malformed-lease-{index}.json"
            manager = ProviderJobManager(
                path, clock=lambda: self.now[0], lease_seconds=10
            )
            manager.submit(
                job_id="job-1", project_id="film", provider_id="image",
                payload_ref="payload:job-1",
            )
            claimed = manager.claim_next(worker_id="worker-1")
            self.assertEqual(claimed["status"], "JOB_CLAIMED")
            manager.data["jobs"]["job-1"]["lease_until"] = bad_lease
            manager._save()

            reclaimed = manager.claim_next(worker_id="worker-2")
            self.assertEqual(reclaimed["status"], "JOB_CLAIMED")
            self.assertEqual(reclaimed["job"]["worker_id"], "worker-2")
            self.assertEqual(reclaimed["job"]["attempts"], 2)

    def test_completion_persists_other_expired_jobs_even_when_target_is_missing(self):
        self.submit("job-1")
        self.assertEqual(
            self.manager.claim_next(worker_id="worker-1")["status"],
            "JOB_CLAIMED",
        )
        self.now[0] += timedelta(seconds=11)
        missing = self.manager.complete(
            job_id="missing-job", worker_id="worker-1",
            lease_token="A" * 43, success=False,
        )
        self.assertEqual(missing["status"], "JOB_NOT_FOUND")
        persisted = self.manager.poll(job_id="job-1")
        self.assertEqual(persisted["job"]["status"], "QUEUED")
        self.assertEqual(persisted["job"]["last_error_code"], "WORKER_LEASE_EXPIRED")

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
