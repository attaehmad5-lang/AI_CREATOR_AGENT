"""Queue capacity, idempotency, async lifecycle, and backpressure tests."""
import hashlib
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

    def test_job_trace_id_is_generated_persisted_and_stable_for_idempotent_retries(self):
        submitted = self.submit("trace-job")
        self.assertTrue(submitted["accepted"])
        trace_id = submitted["job"]["trace_id"]
        self.assertRegex(trace_id, r"^[0-9a-f]{32}$")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["jobs"]["trace-job"]["trace_id"], trace_id)

        duplicate = self.submit("trace-job")
        self.assertEqual(duplicate["status"], "ALREADY_SUBMITTED")
        self.assertEqual(duplicate["job"]["trace_id"], trace_id)

        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.poll(job_id="trace-job")["job"]["trace_id"], trace_id)

    def test_job_trace_id_validation_and_explicit_idempotency_binding(self):
        invalid = self.manager.submit(
            job_id="bad-trace", project_id="film", provider_id="image",
            payload_ref="payload:bad-trace", trace_id="not-a-trace",
        )
        self.assertEqual(invalid["status"], "INVALID_TRACE_ID")

        supplied = "a" * 32
        created = self.manager.submit(
            job_id="explicit-trace", project_id="film", provider_id="image",
            payload_ref="payload:explicit-trace", trace_id=supplied,
        )
        self.assertEqual(created["job"]["trace_id"], supplied)
        conflict = self.manager.submit(
            job_id="explicit-trace", project_id="film", provider_id="image",
            payload_ref="payload:explicit-trace", trace_id="b" * 32,
        )
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")

    def test_claim_next_filters_jobs_by_allowed_provider_ids_before_leasing(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=5, max_concurrent=2,
            scheduling_strategy="GLOBAL_PRIORITY",
            clock=lambda: self.now[0],
        )
        image_submission = manager.submit(
            job_id="image-authorized", project_id="film", provider_id="image",
            payload_ref="payload:image-authorized", priority=100,
        )
        video_submission = manager.submit(
            job_id="video-not-authorized", project_id="film", provider_id="video",
            payload_ref="payload:video-not-authorized", priority=99,
        )
        self.assertTrue(image_submission["accepted"], image_submission)
        self.assertTrue(video_submission["accepted"], video_submission)
        self.assertEqual(
            manager.poll(job_id="video-not-authorized")["job"]["status"], "QUEUED"
        )
        claimed = manager.claim_next(
            worker_id="worker-1", allowed_provider_ids=["image"]
        )
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["provider_id"], "image")
        self.assertEqual(
            manager.poll(job_id="video-not-authorized")["job"]["status"], "QUEUED"
        )

        blocked = manager.claim_next(
            worker_id="worker-2", allowed_provider_ids=["image"]
        )
        self.assertEqual(blocked["status"], "NO_AUTHORIZED_PROVIDER_JOBS")
        self.assertEqual(manager.poll(job_id="video-not-authorized")["job"]["status"], "QUEUED")

    def test_claim_next_rejects_invalid_allowed_provider_filter(self):
        manager = ProviderJobManager(self.path, clock=lambda: self.now[0])
        for allowed in ("image", ["../outside"], ["image", "image"], [None]):
            with self.subTest(allowed=allowed):
                self.assertEqual(
                    manager.claim_next(
                        worker_id="worker-1", allowed_provider_ids=allowed
                    )["status"],
                    "INVALID_ALLOWED_PROVIDER_IDS",
                )

    def test_provider_specific_concurrency_limits_skip_capped_providers(self):
        manager = ProviderJobManager(
            self.path,
            max_queue_size=10,
            max_concurrent=4,
            lease_seconds=10,
            max_attempts=2,
            scheduling_strategy="GLOBAL_PRIORITY",
            provider_concurrency_limits={"image": 1, "video": 2},
            clock=lambda: self.now[0],
        )
        submissions = (
            ("image-one", "image", 100),
            ("image-two", "image", 90),
            ("video-one", "video", 80),
            ("video-two", "video", 70),
        )
        for job_id, provider, priority in submissions:
            manager.submit(
                job_id=job_id, project_id="film", provider_id=provider,
                payload_ref=f"payload:{job_id}", priority=priority,
            )

        first = manager.claim_next(worker_id="worker-1")
        second = manager.claim_next(worker_id="worker-2")
        third = manager.claim_next(worker_id="worker-3")
        blocked = manager.claim_next(worker_id="worker-4")
        self.assertEqual(first["job"]["job_id"], "image-one")
        self.assertEqual(second["job"]["job_id"], "video-one")
        self.assertEqual(third["job"]["job_id"], "video-two")
        self.assertEqual(blocked["status"], "PROVIDER_CONCURRENCY_LIMIT")
        self.assertEqual(blocked["queued"], 1)

        released = manager.complete(
            job_id="image-one", worker_id="worker-1",
            lease_token=first["job"]["lease_token"], success=False,
            error_code="TEST_RELEASE_PROVIDER_SLOT",
        )
        self.assertTrue(released["saved"])
        reopened = manager.claim_next(worker_id="worker-4")
        self.assertEqual(reopened["status"], "JOB_CLAIMED")
        self.assertEqual(reopened["job"]["job_id"], "image-two")

    def test_provider_specific_concurrency_limit_configuration_is_validated(self):
        for limits in (
            {"../provider": 1},
            {"image": 0},
            {"image": True},
            {"image": 3},
            ["image"],
        ):
            with self.subTest(limits=limits):
                with self.assertRaises(ValueError):
                    ProviderJobManager(
                        self.path,
                        max_concurrent=2,
                        provider_concurrency_limits=limits,
                        clock=lambda: self.now[0],
                    )

    def test_persisted_running_job_requires_well_formed_lease_token(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        persisted["jobs"]["job-1"].pop("lease_token_hash", None)
        persisted["jobs"]["job-1"]["lease_token"] = "bad-token"
        self.path.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "provider job record"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_persisted_running_job_requires_lease_identity_pair(self):
        self.submit("job-1")
        self.manager.claim_next(worker_id="worker-1")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        persisted["jobs"]["job-1"].pop("lease_token_hash", None)
        persisted["jobs"]["job-1"]["lease_token"] = None
        self.path.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "provider job record"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_persisted_running_job_requires_valid_worker_id(self):
        self.submit("job-1")
        self.manager.claim_next(worker_id="worker-1")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        persisted["jobs"]["job-1"]["worker_id"] = "../invalid"
        self.path.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "provider job record"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_persisted_running_job_accepts_valid_lease_identity(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.data["jobs"]["job-1"]["worker_id"], "worker-1")
        self.assertNotIn("lease_token", reloaded.data["jobs"]["job-1"])
        self.assertEqual(
            reloaded.data["jobs"]["job-1"]["lease_token_hash"],
            hashlib.sha256(claimed["job"]["lease_token"].encode("ascii")).hexdigest(),
        )

    def test_completed_job_clears_worker_lease_identity(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        completed = self.manager.complete(
            job_id="job-1", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], success=False,
            error_code="TEST_DONE",
        )
        self.assertEqual(completed["status"], "JOB_COMPLETED")
        self.assertIsNone(completed["job"]["worker_id"])
        self.assertIsNone(completed["job"].get("lease_token"))
        self.assertIsNone(completed["job"].get("lease_token_hash"))
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.data["jobs"]["job-1"]["status"], "FAILED")

    def test_legacy_terminal_job_worker_id_remains_backward_compatible(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.manager.complete(
            job_id="job-1", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], success=False,
            error_code="TEST_DONE",
        )
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        persisted["jobs"]["job-1"]["worker_id"] = "worker-1"
        self.path.write_text(json.dumps(persisted), encoding="utf-8")
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(reloaded.data["jobs"]["job-1"]["worker_id"], "worker-1")
        self.assertIsNone(reloaded.data["jobs"]["job-1"].get("lease_token"))
        self.assertIsNone(reloaded.data["jobs"]["job-1"].get("lease_token_hash"))

    def test_persisted_queued_job_accepts_null_lease_token(self):
        self.submit("job-1")
        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertIsNone(reloaded.data["jobs"]["job-1"].get("lease_token"))
        self.assertIsNone(reloaded.data["jobs"]["job-1"].get("lease_token_hash"))
        self.assertIsNone(reloaded.data["jobs"]["job-1"].get("worker_id"))


    def test_running_lease_token_is_hashed_at_rest_and_hidden_from_job_reads(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        token = claimed["job"]["lease_token"]
        self.assertTrue(token)
        persisted_text = self.path.read_text(encoding="utf-8")
        persisted_job = json.loads(persisted_text)["jobs"]["job-1"]
        self.assertNotIn(token, persisted_text)
        self.assertNotIn("lease_token", persisted_job)
        self.assertEqual(
            persisted_job["lease_token_hash"],
            hashlib.sha256(token.encode("ascii")).hexdigest(),
        )

        polled = self.manager.poll(job_id="job-1")
        self.assertEqual(polled["job"]["status"], "RUNNING")
        self.assertNotIn("lease_token", polled["job"])
        self.assertNotIn("lease_token_hash", polled["job"])
        renewed = self.manager.renew_lease(
            job_id="job-1", worker_id="worker-1", lease_token=token,
        )
        self.assertEqual(renewed["status"], "JOB_LEASE_RENEWED")
        self.assertNotIn("lease_token_hash", renewed["job"])
        finished = self.manager.complete(
            job_id="job-1", worker_id="worker-1", lease_token=token,
            success=False, error_code="TEST_DONE",
        )
        self.assertEqual(finished["status"], "JOB_COMPLETED")
        persisted_after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(persisted_after["jobs"]["job-1"]["lease_token_hash"])

    def test_legacy_plaintext_running_lease_migrates_without_invalidating_worker(self):
        self.submit("job-1")
        claimed = self.manager.claim_next(worker_id="worker-1")
        token = claimed["job"]["lease_token"]
        legacy = json.loads(self.path.read_text(encoding="utf-8"))
        legacy_job = legacy["jobs"]["job-1"]
        legacy_job.pop("lease_token_hash", None)
        legacy_job["lease_token"] = token
        self.path.write_text(json.dumps(legacy), encoding="utf-8")

        reloaded = ProviderJobManager(self.path, clock=lambda: self.now[0])
        in_memory_job = reloaded.data["jobs"]["job-1"]
        self.assertNotIn("lease_token", in_memory_job)
        self.assertEqual(
            in_memory_job["lease_token_hash"],
            hashlib.sha256(token.encode("ascii")).hexdigest(),
        )
        renewed = reloaded.renew_lease(
            job_id="job-1", worker_id="worker-1", lease_token=token,
        )
        self.assertEqual(renewed["status"], "JOB_LEASE_RENEWED")
        migrated_text = self.path.read_text(encoding="utf-8")
        migrated_job = json.loads(migrated_text)["jobs"]["job-1"]
        self.assertNotIn(token, migrated_text)
        self.assertNotIn("lease_token", migrated_job)
        self.assertEqual(
            migrated_job["lease_token_hash"],
            hashlib.sha256(token.encode("ascii")).hexdigest(),
        )
        completed = reloaded.complete(
            job_id="job-1", worker_id="worker-1", lease_token=token,
            success=True, result_ref="asset:migrated",
        )
        self.assertEqual(completed["status"], "JOB_COMPLETED")


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

    def test_project_round_robin_prevents_one_project_from_monopolizing_queue(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=1,
            scheduling_strategy="PROJECT_ROUND_ROBIN",
            clock=lambda: self.now[0],
        )
        for job_id in ("alpha-1", "alpha-2", "alpha-3", "alpha-4"):
            manager.submit(
                job_id=job_id, project_id="alpha", provider_id="image",
                payload_ref=f"payload:{job_id}", priority=100,
            )
        for job_id in ("beta-1", "beta-2", "beta-3"):
            manager.submit(
                job_id=job_id, project_id="beta", provider_id="image",
                payload_ref=f"payload:{job_id}", priority=1,
            )

        claimed_projects = []
        claimed_jobs = []
        for index in range(4):
            claimed = manager.claim_next(worker_id=f"worker-{index}")
            self.assertEqual(claimed["status"], "JOB_CLAIMED")
            claimed_projects.append(claimed["job"]["project_id"])
            claimed_jobs.append(claimed["job"]["job_id"])
            manager.complete(
                job_id=claimed["job"]["job_id"],
                worker_id=f"worker-{index}",
                lease_token=claimed["job"]["lease_token"],
                success=False,
                error_code="TEST_DONE",
            )
        self.assertEqual(claimed_projects, ["alpha", "beta", "alpha", "beta"])
        self.assertEqual(claimed_jobs, ["alpha-1", "beta-1", "alpha-2", "beta-2"])

    def test_fair_scheduler_keeps_priority_order_within_each_project(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=1,
            scheduling_strategy="PROJECT_ROUND_ROBIN",
            clock=lambda: self.now[0],
        )
        for job_id, project, priority in (
            ("alpha-low", "alpha", 1),
            ("alpha-high", "alpha", 90),
            ("beta-mid", "beta", 40),
        ):
            manager.submit(
                job_id=job_id, project_id=project, provider_id="image",
                payload_ref=f"payload:{job_id}", priority=priority,
            )
        first = manager.claim_next(worker_id="worker-1")
        self.assertEqual(first["job"]["job_id"], "alpha-high")
        manager.complete(
            job_id=first["job"]["job_id"], worker_id="worker-1",
            lease_token=first["job"]["lease_token"], success=False,
            error_code="TEST_DONE",
        )
        second = manager.claim_next(worker_id="worker-2")
        self.assertEqual(second["job"]["job_id"], "beta-mid")

    def test_fair_scheduler_cursor_persists_across_manager_restart(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=1,
            clock=lambda: self.now[0],
        )
        manager.submit(
            job_id="alpha-1", project_id="alpha", provider_id="image",
            payload_ref="payload:alpha-1",
        )
        manager.submit(
            job_id="beta-1", project_id="beta", provider_id="image",
            payload_ref="payload:beta-1",
        )
        first = manager.claim_next(worker_id="worker-1")
        self.assertEqual(first["job"]["project_id"], "alpha")
        manager.complete(
            job_id="alpha-1", worker_id="worker-1",
            lease_token=first["job"]["lease_token"], success=False,
            error_code="TEST_DONE",
        )

        restarted = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=1,
            clock=lambda: self.now[0],
        )
        second = restarted.claim_next(worker_id="worker-2")
        self.assertEqual(second["job"]["project_id"], "beta")
        self.assertEqual(restarted.data["scheduler"]["last_project_id"], "beta")

    def test_global_priority_mode_preserves_legacy_cross_project_order(self):
        manager = ProviderJobManager(
            self.path, max_queue_size=10, max_concurrent=1,
            scheduling_strategy="GLOBAL_PRIORITY",
            clock=lambda: self.now[0],
        )
        manager.submit(
            job_id="alpha-low", project_id="alpha", provider_id="image",
            payload_ref="payload:alpha-low", priority=10,
        )
        manager.submit(
            job_id="beta-high", project_id="beta", provider_id="image",
            payload_ref="payload:beta-high", priority=90,
        )
        claimed = manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["job"]["job_id"], "beta-high")

    def test_legacy_job_state_without_scheduler_cursor_loads_and_migrates(self):
        self.path.write_text(json.dumps({
            "version": "1.0",
            "jobs": {
                "alpha-1": {
                    "job_id": "alpha-1", "project_id": "alpha",
                    "provider_id": "image", "payload_ref": "payload:alpha-1",
                    "priority": 50, "status": "QUEUED", "attempts": 0,
                    "created_at": self.now[0].isoformat(),
                    "updated_at": self.now[0].isoformat(),
                    "cancel_requested": False,
                },
            },
        }), encoding="utf-8")
        manager = ProviderJobManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(manager.data["scheduler"], {"last_project_id": None})
        claimed = manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["scheduler"]["last_project_id"], "alpha")

    def test_invalid_fair_scheduling_configuration_and_persisted_cursor_fail_closed(self):
        with self.assertRaises(ValueError):
            ProviderJobManager(self.path, scheduling_strategy="UNFAIR")
        self.path.write_text(json.dumps({
            "version": "1.0", "jobs": {},
            "scheduler": {"last_project_id": "../invalid"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scheduler state"):
            ProviderJobManager(self.path)

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


    def test_queued_job_deadline_expires_before_worker_claim(self):
        deadline = (self.now[0] + timedelta(seconds=5)).isoformat()
        result = self.manager.submit(
            job_id="deadline-queued", project_id="film", provider_id="image",
            payload_ref="payload:deadline-queued", deadline_at=deadline,
        )
        self.assertTrue(result["accepted"])
        self.now[0] += timedelta(seconds=6)
        self.assertEqual(self.manager.claim_next(worker_id="worker-1")["status"], "QUEUE_EMPTY")
        job = self.manager.poll(job_id="deadline-queued")["job"]
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["error_code"], "JOB_DEADLINE_EXCEEDED")

    def test_running_job_deadline_stops_worker_and_clears_lease(self):
        deadline = (self.now[0] + timedelta(seconds=5)).isoformat()
        self.manager.submit(
            job_id="deadline-running", project_id="film", provider_id="image",
            payload_ref="payload:deadline-running", deadline_at=deadline,
        )
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(
            self.manager.check_worker_control(
                job_id="deadline-running", worker_id="worker-1",
                lease_token=claimed["job"]["lease_token"],
            )["status"],
            "WORKER_CONTINUE",
        )
        self.now[0] += timedelta(seconds=6)
        stopped = self.manager.check_worker_control(
            job_id="deadline-running", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"],
        )
        self.assertEqual(stopped["status"], "JOB_DEADLINE_EXCEEDED")
        self.assertTrue(stopped["should_stop"])
        persisted = self.manager.poll(job_id="deadline-running")["job"]
        self.assertEqual(persisted["status"], "FAILED")
        self.assertIsNone(persisted["worker_id"])

    def test_worker_checkpoint_finalizes_cooperative_cancellation(self):
        self.submit("cancel-checkpoint")
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(
            self.manager.cancel(job_id="cancel-checkpoint")["status"],
            "CANCEL_REQUESTED",
        )
        stopped = self.manager.check_worker_control(
            job_id="cancel-checkpoint", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"],
        )
        self.assertEqual(stopped["status"], "JOB_CANCELLED")
        self.assertTrue(stopped["should_stop"])
        self.assertEqual(
            self.manager.poll(job_id="cancel-checkpoint")["job"]["status"],
            "CANCELLED",
        )

    def test_invalid_deadlines_fail_closed(self):
        for invalid in ("2026-10-16T12:00:00", "not-a-date", "x" * 65):
            result = self.manager.submit(
                job_id="invalid-deadline", project_id="film", provider_id="image",
                payload_ref="payload:invalid-deadline", deadline_at=invalid,
            )
            self.assertEqual(result["status"], "INVALID_DEADLINE")
        expired = self.manager.submit(
            job_id="already-expired", project_id="film", provider_id="image",
            payload_ref="payload:already-expired",
            deadline_at=(self.now[0] - timedelta(seconds=1)).isoformat(),
        )
        self.assertEqual(expired["status"], "DEADLINE_ALREADY_EXPIRED")

    def test_graceful_stop_is_shared_across_manager_instances(self):
        self.submit("shared-before-stop")
        stopped = self.manager.stop_accepting()
        self.assertFalse(stopped["accepting"])

        other = ProviderJobManager(
            self.path, max_queue_size=2, max_concurrent=1,
            lease_seconds=10, max_attempts=2,
            clock=lambda: self.now[0],
        )
        self.assertFalse(other.drain_status()["accepting"])
        rejected = other.submit(
            job_id="shared-rejected", project_id="film", provider_id="image",
            payload_ref="payload:shared-rejected",
        )
        self.assertEqual(rejected["status"], "QUEUE_SHUTTING_DOWN")

        resumed = other.resume_accepting()
        self.assertTrue(resumed["accepting"])
        accepted = self.manager.submit(
            job_id="shared-after-resume", project_id="film", provider_id="image",
            payload_ref="payload:shared-after-resume",
        )
        self.assertTrue(accepted["accepted"])

    def test_queue_control_state_validation_rejects_inconsistent_persisted_state(self):
        self.manager.stop_accepting()
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        persisted["control"]["accepting"] = True
        with self.path.open("w", encoding="utf-8") as stream:
            json.dump(persisted, stream)
        with self.assertRaisesRegex(ValueError, "queue control state"):
            ProviderJobManager(self.path, clock=lambda: self.now[0])

    def test_graceful_stop_rejects_new_jobs_and_reports_drain(self):
        self.submit("accepted-before-stop")
        stopped = self.manager.stop_accepting()
        self.assertFalse(stopped["accepting"])
        self.assertEqual(stopped["queued"], 1)
        self.assertEqual(self.manager.submit(
            job_id="rejected-after-stop", project_id="film", provider_id="image",
            payload_ref="payload:rejected-after-stop",
        )["status"], "QUEUE_SHUTTING_DOWN")
        claimed = self.manager.claim_next(worker_id="worker-1")
        result = self.manager.complete(
            job_id="accepted-before-stop", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:drained",
        )
        self.assertTrue(result["saved"])
        self.assertTrue(self.manager.drain_status()["drained"])
        self.assertTrue(self.manager.resume_accepting()["accepting"])

if __name__ == "__main__":
    unittest.main()
