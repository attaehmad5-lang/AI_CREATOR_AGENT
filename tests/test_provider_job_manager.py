"""Queue capacity, idempotency, async lifecycle, and backpressure tests."""
import tempfile
from concurrent.futures import ThreadPoolExecutor
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

    def test_naive_clock_is_rejected_before_job_mutation(self):
        manager = ProviderJobManager(
            self.path,
            clock=lambda: datetime(2026, 10, 16, 12),
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware datetime"):
            manager.submit(
                job_id="job-naive-clock", project_id="film",
                provider_id="image", payload_ref="payload:job-naive-clock",
            )
        self.assertFalse(self.path.exists())

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

    def test_fair_scheduling_round_robins_projects_and_preserves_priority_within_project(self):
        fair = ProviderJobManager(
            self.path, max_concurrent=10, fair_scheduling=True,
            clock=lambda: self.now[0],
        )
        submissions = [
            ("film-a-1", "film-a", 10),
            ("film-a-2", "film-a", 99),
            ("film-a-3", "film-a", 50),
            ("film-b-1", "film-b", 20),
            ("film-b-2", "film-b", 80),
        ]
        for job_id, project_id, priority in submissions:
            result = fair.submit(
                job_id=job_id, project_id=project_id, provider_id="image",
                payload_ref=f"payload:{job_id}", priority=priority,
            )
            self.assertTrue(result["accepted"])

        claimed = [
            fair.claim_next(worker_id=f"worker-{index}")["job"]
            for index in range(1, 6)
        ]
        self.assertEqual(
            [job["project_id"] for job in claimed],
            ["film-a", "film-b", "film-a", "film-b", "film-a"],
        )
        self.assertEqual(
            [job["job_id"] for job in claimed],
            ["film-a-2", "film-b-2", "film-a-3", "film-b-1", "film-a-1"],
        )
        reloaded = ProviderJobManager(
            self.path, max_concurrent=10, fair_scheduling=True,
            clock=lambda: self.now[0],
        )
        self.assertEqual(reloaded.data["last_scheduled_project"], "film-a")

    def test_fair_scheduling_flag_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "fair_scheduling"):
            ProviderJobManager(self.path, fair_scheduling="yes")

    def test_concurrent_load_respects_queue_capacity_and_worker_limit(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=50, max_concurrent=4,
            clock=lambda: self.now[0],
        )
        def submit(index):
            return manager.submit(
                job_id=f"load-job-{index:03d}",
                project_id=f"project-{index % 5}",
                provider_id="provider-a",
                payload_ref=f"payload:{index}",
                priority=index % 100,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(submit, range(120)))
        accepted = [item for item in results if item["accepted"]]
        rejected = [item for item in results if not item["accepted"]]
        self.assertEqual(len(accepted), 50)
        self.assertEqual(len(rejected), 70)
        self.assertTrue(all(item["status"] == "QUEUE_FULL" for item in rejected))

        reloaded = ProviderJobManager(
            self.path, max_queue_size=50, max_concurrent=4,
            clock=lambda: self.now[0],
        )
        self.assertEqual(
            sum(job["status"] == "QUEUED" for job in reloaded.data["jobs"].values()),
            50,
        )
        claimed = [
            reloaded.claim_next(worker_id=f"load-worker-{index}")
            for index in range(4)
        ]
        self.assertTrue(all(item["status"] == "JOB_CLAIMED" for item in claimed))
        self.assertEqual(
            reloaded.claim_next(worker_id="load-worker-overflow")["status"],
            "CONCURRENCY_LIMIT",
        )

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
            job_id="job-1", worker_id="worker-1", success=True,
            result_ref="asset:result-1",
        )
        self.assertEqual(done["job"]["status"], "SUCCEEDED")
        self.assertIsNone(done["job"]["worker_id"])
        self.assertIsNone(done["job"]["lease_until"])
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

    def test_running_job_requires_valid_lease_and_worker_metadata(self):
        import json
        malformed = {
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "status": "RUNNING",
                    "attempts": 1,
                    "priority": 50,
                    "worker_id": "worker-1",
                    "lease_until": None,
                }
            },
        }
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job lifecycle metadata"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_invalid_persisted_worker_metadata_is_rejected(self):
        import json
        malformed = {
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "status": "RUNNING",
                    "attempts": 1,
                    "priority": 50,
                    "cancel_requested": "false",
                    "worker_id": "../outside",
                }
            },
        }
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job lifecycle metadata"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_malformed_persisted_job_fields_fail_with_schema_error(self):
        malformed = {
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "created_at": "2026-10-16T12:00:00+00:00",
                    "updated_at": "2026-10-16T12:00:00+00:00",
                    "status": [],
                    "attempts": 0,
                    "priority": 50,
                }
            },
        }
        import json
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job record"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_persisted_timestamps_must_be_valid_and_timezone_aware(self):
        import json
        malformed = {
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "status": "QUEUED",
                    "attempts": 0,
                    "priority": 50,
                    "created_at": "2026-10-16T12:00:00+00:00",
                    "updated_at": "not-a-timestamp",
                    "cancel_requested": False,
                }
            },
        }
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job timestamp"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_running_job_with_malformed_attempt_count_fails_closed(self):
        import json
        malformed = {
            "version": "1.0",
            "jobs": {
                "job-1": {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "status": "RUNNING",
                    "attempts": "one",
                    "priority": 50,
                    "worker_id": "worker-1",
                    "lease_until": "2026-10-16T12:00:10+00:00",
                    "started_at": "2026-10-16T12:00:00+00:00",
                    "created_at": "2026-10-16T12:00:00+00:00",
                    "updated_at": "2026-10-16T12:00:00+00:00",
                    "cancel_requested": False,
                }
            },
        }
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job lifecycle metadata"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_persisted_error_and_result_references_are_validated(self):
        import json
        malformed_jobs = [
            {
                "status": "QUEUED", "attempts": 0,
                "last_error_code": "../secret",
            },
            {
                "status": "SUCCEEDED", "attempts": 1,
                "completed_at": "2026-10-16T12:00:00+00:00",
                "result_ref": "../outside",
            },
            {
                "status": "FAILED", "attempts": 1,
                "completed_at": "2026-10-16T12:00:00+00:00",
                "result_ref": "asset:unexpected",
            },
        ]
        for fields in malformed_jobs:
            with self.subTest(fields=fields):
                job = {
                    "job_id": "job-1",
                    "project_id": "film",
                    "provider_id": "image",
                    "payload_ref": "payload:job-1",
                    "priority": 50,
                    "created_at": "2026-10-16T12:00:00+00:00",
                    "updated_at": "2026-10-16T12:00:00+00:00",
                    "cancel_requested": False,
                    **fields,
                }
                self.path.write_text(
                    json.dumps({"version": "1.0", "jobs": {"job-1": job}}),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "invalid provider job record"):
                    ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_unsupported_state_schema_version_is_rejected(self):
        self.path.write_text(
            '{"version":"99.0","jobs":{}}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "invalid provider job state schema"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_state_file_symlink_is_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "outside.json"
            target.write_text('{"version":"1.0","jobs":{}}', encoding="utf-8")
            link = root / "jobs.json"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable on this platform")
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                ProviderJobManager(link)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                '{"version":"1.0","jobs":{}}',
            )

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(self.manager.submit(
            job_id="bad id", project_id="film", provider_id="image",
            payload_ref="payload:bad",
        )["status"], "INVALID_JOB_ID")
        self.assertEqual(self.manager.claim_next(worker_id="bad worker")["status"], "INVALID_WORKER_ID")
        self.assertEqual(self.manager.poll(job_id="missing")["status"], "JOB_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
