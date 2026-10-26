"""Queue capacity, idempotency, async lifecycle, and backpressure tests."""
import tempfile
import threading
import time
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

    def test_concurrent_queue_load_completes_every_job_once(self):
        from concurrent.futures import ThreadPoolExecutor

        manager = ProviderJobManager(
            self.path, max_queue_size=100, max_concurrent=4,
            clock=lambda: self.now[0],
        )
        total = 60
        for index in range(total):
            result = manager.submit(
                job_id=f"load-{index}", project_id="load-project",
                provider_id="image", payload_ref=f"payload:load-{index}",
                priority=index % 5,
            )
            self.assertEqual(result["status"], "JOB_QUEUED")

        completed = []
        completion_lock = threading.Lock()

        def worker(worker_index):
            while True:
                claimed = manager.claim_next(worker_id=f"load-worker-{worker_index}")
                if claimed["status"] == "QUEUE_EMPTY":
                    return
                if claimed["status"] == "CONCURRENCY_LIMIT":
                    time.sleep(0.001)
                    continue
                self.assertEqual(claimed["status"], "JOB_CLAIMED")
                job = claimed["job"]
                result = manager.complete(
                    job_id=job["job_id"],
                    worker_id=f"load-worker-{worker_index}",
                    success=True,
                    result_ref=f"asset:{job['job_id']}",
                )
                self.assertEqual(result["status"], "JOB_COMPLETED")
                with completion_lock:
                    completed.append(job["job_id"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))

        self.assertEqual(len(completed), total)
        self.assertEqual(len(set(completed)), total)
        statuses = [manager.poll(job_id=f"load-{index}")["job"]["status"] for index in range(total)]
        self.assertEqual(set(statuses), {"SUCCEEDED"})

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

    def test_expired_cancelled_worker_lease_finishes_as_cancelled(self):
        self.submit("cancel-expired")
        self.assertEqual(
            self.manager.claim_next(worker_id="worker-old")["status"],
            "JOB_CLAIMED",
        )
        self.assertEqual(
            self.manager.cancel(job_id="cancel-expired")["status"],
            "CANCEL_REQUESTED",
        )
        self.now[0] += timedelta(seconds=11)

        next_claim = self.manager.claim_next(worker_id="worker-new")
        self.assertEqual(next_claim["status"], "QUEUE_EMPTY")
        current = self.manager.poll(job_id="cancel-expired")["job"]
        self.assertEqual(current["status"], "CANCELLED")
        self.assertEqual(current["error_code"] if "error_code" in current else None, None)
        self.assertIsNone(current["lease_until"])

    def test_expired_worker_cannot_complete_before_retry_is_claimed(self):
        self.submit("stale-worker")
        claimed = self.manager.claim_next(worker_id="worker-old")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.now[0] += timedelta(seconds=11)

        stale = self.manager.complete(
            job_id="stale-worker", worker_id="worker-old",
            success=True, result_ref="asset:stale-result",
        )
        self.assertEqual(stale["status"], "JOB_LEASE_EXPIRED")
        self.assertFalse(stale["saved"])
        current = self.manager.poll(job_id="stale-worker")["job"]
        self.assertEqual(current["status"], "QUEUED")
        self.assertNotIn("result_ref", current)
        self.assertEqual(current["last_error_code"], "WORKER_LEASE_EXPIRED")

    def test_invalid_worker_leases_are_reaped_without_crashing(self):
        # Persisted timestamps can be damaged by old versions, manual edits, or
        # interrupted external tooling. Invalid leases must not strand RUNNING jobs.
        for lease_value, omit_lease in (
            ("2026-10-16T12:00:00", False),  # naive timestamp
            ("not-a-timestamp", False),
            (None, True),  # missing lease
        ):
            with self.subTest(lease_value=lease_value, omit_lease=omit_lease):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "jobs.json"
                    manager = ProviderJobManager(
                        path, max_queue_size=5, max_concurrent=1,
                        lease_seconds=10, max_attempts=3,
                        clock=lambda: datetime(2026, 10, 16, 12, tzinfo=timezone.utc),
                    )
                    self.assertTrue(manager.submit(
                        job_id="recover-me", project_id="film",
                        provider_id="image", payload_ref="payload:recover-me",
                    )["accepted"])
                    self.assertEqual(
                        manager.claim_next(worker_id="worker-old")["status"],
                        "JOB_CLAIMED",
                    )
                    import json
                    state = json.loads(path.read_text(encoding="utf-8"))
                    job = state["jobs"]["recover-me"]
                    if omit_lease:
                        job.pop("lease_until", None)
                    else:
                        job["lease_until"] = lease_value
                    path.write_text(json.dumps(state), encoding="utf-8")

                    recovered = manager.claim_next(worker_id="worker-new")
                    self.assertEqual(recovered["status"], "JOB_CLAIMED")
                    self.assertEqual(recovered["job"]["job_id"], "recover-me")
                    self.assertEqual(recovered["job"]["attempts"], 2)
                    self.assertEqual(
                        recovered["job"]["last_error_code"], "WORKER_LEASE_EXPIRED"
                    )

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
