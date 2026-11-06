"""Remote provider job submission, polling, cancellation and recovery tests."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_job_manager import ProviderJobManager
from projects.provider_store.provider_service import ProviderService


class StubConnectionManager:
    def verify_connectivity(self, *args, **kwargs):
        return {"verified": True}


class StubRemoteAdapter:
    def __init__(self):
        self.submit_calls = []
        self.poll_calls = []
        self.cancel_calls = []
        self.submit_responses = []
        self.poll_responses = []
        self.cancel_responses = []
        self.submit_error = None
        self.poll_error = None
        self.cancel_error = None

    def submit_remote_job(self, *, job_id, project_id, payload_ref, idempotency_key):
        self.submit_calls.append({
            "job_id": job_id,
            "project_id": project_id,
            "payload_ref": payload_ref,
            "idempotency_key": idempotency_key,
        })
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_responses:
            return self.submit_responses.pop(0)
        return {
            "remote_job_id": "remote-job-001",
            "status": "QUEUED",
            "poll_after_seconds": 10,
            "provider_debug": "must never escape",
        }

    def poll_remote_job(self, *, remote_job_id):
        self.poll_calls.append(remote_job_id)
        if self.poll_error is not None:
            raise self.poll_error
        if self.poll_responses:
            return self.poll_responses.pop(0)
        return {"status": "RUNNING", "poll_after_seconds": 15}

    def cancel_remote_job(self, *, remote_job_id):
        self.cancel_calls.append(remote_job_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        if self.cancel_responses:
            return self.cancel_responses.pop(0)
        return {"status": "CANCELLED", "provider_debug": "must never escape"}


class RemoteProviderJobServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = [datetime(2026, 11, 6, 12, tzinfo=timezone.utc)]
        self.jobs_path = self.root / "jobs.json"
        self.jobs = ProviderJobManager(
            self.jobs_path,
            max_queue_size=8,
            max_concurrent=2,
            lease_seconds=30,
            max_attempts=3,
            clock=lambda: self.now[0],
        )
        self.authorization = ProviderAuthorizationManager(
            self.root / "authorization.json",
            clock=lambda: self.now[0],
        )
        self.authorization.assign_role("creator-1", "creator")
        self.authorization.assign_role("viewer-1", "viewer")
        self.adapter = StubRemoteAdapter()
        self.service = ProviderService(
            StubConnectionManager(),
            job_manager=self.jobs,
            authorization_manager=self.authorization,
            remote_job_adapters={"image": self.adapter},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def submit_and_claim(self, *, idempotency_key="client-retry-key-001", job_id="local-job-1"):
        accepted = self.service.submit_job(
            job_id=job_id,
            project_id="film",
            provider_id="image",
            payload_ref=f"payload:{job_id}",
            priority=60,
            principal_id="creator-1",
            idempotency_key=idempotency_key,
        )
        self.assertEqual(accepted["status"], "JOB_QUEUED")
        claimed = self.service.claim_next_job(
            worker_id="worker-1",
            principal_id="creator-1",
        )
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        return claimed["job"]

    def test_remote_submission_persists_remote_identity_and_stable_hashed_key(self):
        worker_job = self.submit_and_claim()
        result = self.service.submit_remote_job(
            job_id=worker_job["job_id"],
            worker_id="worker-1",
            lease_token=worker_job["lease_token"],
            principal_id="creator-1",
        )
        self.assertEqual(result["status"], "REMOTE_JOB_SUBMITTED")
        self.assertTrue(result["saved"])
        self.assertEqual(result["remote_job_id"], "remote-job-001")
        self.assertEqual(len(self.adapter.submit_calls), 1)
        key = self.adapter.submit_calls[0]["idempotency_key"]
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertNotEqual(key, "client-retry-key-001")

        persisted = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        record = persisted["jobs"]["local-job-1"]
        self.assertEqual(record["remote_submission_state"], "SUBMITTED")
        self.assertEqual(record["remote_job_id"], "remote-job-001")
        self.assertEqual(record["remote_status"], "QUEUED")
        self.assertEqual(record["remote_poll_after_seconds"], 10)
        self.assertNotIn("lease_token", record)
        self.assertNotIn(worker_job["lease_token"], self.jobs_path.read_text(encoding="utf-8"))
        self.assertRegex(record["lease_token_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn("client-retry-key-001", self.jobs_path.read_text(encoding="utf-8"))
        self.assertNotIn("provider_debug", repr(result))

    def test_remote_submission_is_not_issued_twice_after_provider_job_is_known(self):
        worker_job = self.submit_and_claim()
        first = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        second = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(first["status"], "REMOTE_JOB_SUBMITTED")
        self.assertEqual(second["status"], "REMOTE_JOB_ALREADY_SUBMITTED")
        self.assertEqual(len(self.adapter.submit_calls), 1)

    def test_remote_poll_updates_pending_then_terminal_local_state(self):
        worker_job = self.submit_and_claim()
        self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.adapter.poll_responses = [
            {"status": "RUNNING", "poll_after_seconds": 20},
            {"status": "SUCCEEDED", "result_ref": "asset:final-video"},
        ]
        pending = self.service.poll_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(pending["status"], "REMOTE_JOB_PENDING")
        self.assertEqual(pending["job"]["remote_status"], "RUNNING")
        self.assertEqual(pending["job"]["remote_poll_after_seconds"], 20)

        completed = self.service.poll_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(completed["status"], "REMOTE_JOB_TERMINAL")
        self.assertTrue(completed["terminal"])
        self.assertEqual(completed["job"]["status"], "SUCCEEDED")
        self.assertEqual(completed["job"]["result_ref"], "asset:final-video")
        self.assertIsNone(self.jobs.data["jobs"]["local-job-1"]["lease_token_hash"])
        self.assertIsNone(self.jobs.data["jobs"]["local-job-1"]["worker_id"])

    def test_remote_submission_timeout_is_unknown_and_retry_after_reclaim_reuses_key(self):
        worker_job = self.submit_and_claim()
        self.adapter.submit_error = TimeoutError("sensitive provider payload")
        first = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(first["status"], "REMOTE_SUBMISSION_OUTCOME_UNKNOWN")
        self.assertTrue(first["retry_requires_same_idempotency_key"])
        self.assertNotIn("sensitive provider payload", repr(first))
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_submission_state"],
            "UNKNOWN",
        )
        prior_key = self.adapter.submit_calls[0]["idempotency_key"]

        self.now[0] += timedelta(seconds=31)
        reclaimed = self.service.claim_next_job(
            worker_id="worker-1", principal_id="creator-1"
        )
        self.assertEqual(reclaimed["status"], "JOB_CLAIMED")
        self.assertNotEqual(reclaimed["job"]["lease_token"], worker_job["lease_token"])
        self.adapter.submit_error = None
        second = self.service.submit_remote_job(
            job_id="local-job-1",
            worker_id="worker-1",
            lease_token=reclaimed["job"]["lease_token"],
            principal_id="creator-1",
        )
        self.assertEqual(second["status"], "REMOTE_JOB_SUBMITTED")
        self.assertEqual(len(self.adapter.submit_calls), 2)
        self.assertEqual(
            self.adapter.submit_calls[1]["idempotency_key"],
            prior_key,
        )

    def test_same_active_lease_cannot_start_provider_submission_twice(self):
        worker_job = self.submit_and_claim()
        first = self.jobs.begin_remote_submission(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"],
        )
        second = self.jobs.begin_remote_submission(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"],
        )
        self.assertEqual(first["status"], "REMOTE_SUBMISSION_STARTED")
        self.assertEqual(second["status"], "REMOTE_SUBMISSION_IN_PROGRESS")
        persisted = self.jobs.data["jobs"]["local-job-1"]
        self.assertEqual(persisted["remote_submission_attempts"], 1)
        self.assertNotIn("lease_token", persisted)
        self.assertNotEqual(
            persisted["remote_submission_lease_token_hash"],
            worker_job["lease_token"],
        )

    def test_remote_submission_requires_idempotency_before_provider_call(self):
        accepted = self.service.submit_job(
            job_id="no-idempotency",
            project_id="film",
            provider_id="image",
            payload_ref="payload:no-idempotency",
            principal_id="creator-1",
        )
        self.assertEqual(accepted["status"], "JOB_QUEUED")
        claimed = self.service.claim_next_job(
            worker_id="worker-1", principal_id="creator-1"
        )
        result = self.service.submit_remote_job(
            job_id="no-idempotency", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(result["status"], "REMOTE_PROVIDER_IDEMPOTENCY_REQUIRED")
        self.assertEqual(self.adapter.submit_calls, [])
        self.assertEqual(
            self.jobs.data["jobs"]["no-idempotency"].get("remote_submission_state", "NOT_SUBMITTED"),
            "NOT_SUBMITTED",
        )

    def test_wrong_worker_and_unauthorized_principal_cannot_submit_or_poll(self):
        worker_job = self.submit_and_claim()
        wrong_worker = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-2",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(wrong_worker["status"], "JOB_LEASE_NOT_OWNED")
        self.assertEqual(self.adapter.submit_calls, [])

        denied = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="viewer-1",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(self.adapter.submit_calls, [])

    def test_invalid_or_secret_bearing_provider_response_is_not_persisted_or_echoed(self):
        worker_job = self.submit_and_claim()
        secret_text = "provider-private-response-secret"
        self.adapter.submit_responses = [{
            "remote_job_id": "remote-job-001",
            "status": "QUEUED",
            "poll_after_seconds": 10,
            "debug": secret_text,
            "payload": {"access_token": secret_text},
        }]
        result = self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(result["status"], "REMOTE_JOB_SUBMITTED")
        self.assertNotIn(secret_text, repr(result))
        self.assertNotIn(secret_text, self.jobs_path.read_text(encoding="utf-8"))
        self.assertNotIn("debug", repr(result))

        # Invalid status values fail closed and leave the submission outcome explicit.
        worker_job_2 = self.submit_and_claim(
            idempotency_key="second-key-002", job_id="local-job-2"
        )
        self.adapter.submit_responses = [{
            "remote_job_id": "remote-job-002",
            "status": ["QUEUED"],
            "poll_after_seconds": 10,
        }]
        invalid = self.service.submit_remote_job(
            job_id=worker_job_2["job_id"], worker_id="worker-1",
            lease_token=worker_job_2["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(invalid["status"], "REMOTE_SUBMISSION_RESPONSE_INVALID")
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-2"]["remote_submission_state"],
            "UNKNOWN",
        )

    def test_remote_cancellation_requires_provider_confirmation(self):
        worker_job = self.submit_and_claim()
        self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.adapter.cancel_responses = [
            {"status": "PENDING"},
            {"status": "CANCELLED", "debug": "private"},
        ]
        pending = self.service.cancel_remote_job(
            job_id="local-job-1", principal_id="creator-1"
        )
        self.assertEqual(pending["status"], "REMOTE_CANCEL_PENDING")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["status"], "RUNNING")

        confirmed = self.service.cancel_remote_job(
            job_id="local-job-1", principal_id="creator-1"
        )
        self.assertEqual(confirmed["status"], "REMOTE_JOB_CANCELLED")
        self.assertEqual(confirmed["job"]["status"], "CANCELLED")
        self.assertIsNone(self.jobs.data["jobs"]["local-job-1"]["lease_token_hash"])
        self.assertNotIn("private", repr(confirmed))

    def test_unsupported_cancel_and_poll_exceptions_keep_remote_job_open(self):
        worker_job = self.submit_and_claim()
        self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.adapter.poll_error = TimeoutError("private poll body")
        poll = self.service.poll_remote_job(
            job_id="local-job-1", worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(poll["status"], "REMOTE_POLL_OUTCOME_UNKNOWN")
        self.assertNotIn("private poll body", repr(poll))
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["status"], "RUNNING")

        self.adapter.cancel_error = TimeoutError("private cancel body")
        cancel = self.service.cancel_remote_job(
            job_id="local-job-1", principal_id="creator-1"
        )
        self.assertEqual(cancel["status"], "REMOTE_CANCEL_OUTCOME_UNKNOWN")
        self.assertNotIn("private cancel body", repr(cancel))
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["status"], "RUNNING")

    def test_adapter_contract_rejects_non_idempotent_remote_submitter(self):
        class UnsafeAdapter:
            def submit_remote_job(self, *, job_id, project_id, payload_ref):
                return {}
            def poll_remote_job(self, *, remote_job_id):
                return {}

        with self.assertRaisesRegex(ValueError, "submit/poll contract"):
            ProviderService(
                StubConnectionManager(),
                remote_job_adapters={"image": UnsafeAdapter()},
            )


    def test_public_job_reads_redact_remote_submission_worker_and_lease_hash(self):
        worker_job = self.submit_and_claim()
        started = self.jobs.begin_remote_submission(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"],
        )
        self.assertEqual(started["status"], "REMOTE_SUBMISSION_STARTED")
        persisted = self.jobs.data["jobs"]["local-job-1"]
        self.assertRegex(
            persisted["remote_submission_lease_token_hash"], r"^[0-9a-f]{64}$"
        )
        public = self.jobs.poll(job_id="local-job-1")
        self.assertEqual(public["status"], "JOB_STATUS")
        self.assertNotIn(
            "remote_submission_lease_token_hash", public["job"]
        )
        self.assertNotIn("remote_submission_worker_id", public["job"])

    def test_remote_status_cannot_regress_from_running_to_queued(self):
        worker_job = self.submit_and_claim()
        self.service.submit_remote_job(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.adapter.poll_responses = [
            {"status": "RUNNING", "poll_after_seconds": 15},
            {"status": "QUEUED", "poll_after_seconds": 10},
        ]
        running = self.service.poll_remote_job(
            job_id="local-job-1", worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(running["status"], "REMOTE_JOB_PENDING")
        regressed = self.service.poll_remote_job(
            job_id="local-job-1", worker_id="worker-1",
            lease_token=worker_job["lease_token"], principal_id="creator-1",
        )
        self.assertEqual(regressed["status"], "REMOTE_JOB_STATUS_REGRESSION")
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_status"], "RUNNING"
        )

    def test_corrupt_remote_submission_lease_hash_fails_closed_on_reload(self):
        worker_job = self.submit_and_claim()
        self.assertEqual(
            self.jobs.begin_remote_submission(
                job_id=worker_job["job_id"], worker_id="worker-1",
                lease_token=worker_job["lease_token"],
            )["status"],
            "REMOTE_SUBMISSION_STARTED",
        )
        persisted = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        persisted["jobs"]["local-job-1"]["remote_submission_lease_token_hash"] = "not-a-digest"
        self.jobs_path.write_text(json.dumps(persisted), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "provider job record"):
            ProviderJobManager(
                self.jobs_path,
                max_queue_size=8,
                max_concurrent=2,
                lease_seconds=30,
                max_attempts=3,
                clock=lambda: self.now[0],
            )


    def test_expired_worker_cannot_record_remote_submission_before_reclaim(self):
        worker_job = self.submit_and_claim()
        begun = self.jobs.begin_remote_submission(
            job_id=worker_job["job_id"], worker_id="worker-1",
            lease_token=worker_job["lease_token"],
        )
        self.assertEqual(begun["status"], "REMOTE_SUBMISSION_STARTED")
        self.now[0] += timedelta(seconds=31)

        late_response = self.jobs.record_remote_submission(
            job_id=worker_job["job_id"],
            worker_id="worker-1",
            lease_token=worker_job["lease_token"],
            remote_job_id="remote-job-too-late",
            remote_status="QUEUED",
            poll_after_seconds=10,
        )
        self.assertEqual(late_response["status"], "JOB_LEASE_NOT_OWNED")
        record = self.jobs.data["jobs"]["local-job-1"]
        self.assertNotIn("remote_job_id", record)
        self.assertEqual(record["remote_submission_state"], "SUBMITTING")

        reclaimed = self.service.claim_next_job(
            worker_id="worker-2", principal_id="creator-1"
        )
        self.assertEqual(reclaimed["status"], "JOB_CLAIMED")
        retry = self.jobs.begin_remote_submission(
            job_id="local-job-1",
            worker_id="worker-2",
            lease_token=reclaimed["job"]["lease_token"],
        )
        self.assertEqual(retry["status"], "REMOTE_SUBMISSION_STARTED")
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_submission_attempts"], 2
        )


if __name__ == "__main__":
    unittest.main()
