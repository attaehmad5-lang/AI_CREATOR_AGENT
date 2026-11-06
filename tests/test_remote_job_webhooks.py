"""Signed provider webhook to asynchronous remote-job synchronization tests."""
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_job_manager import ProviderJobManager
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_webhook_verifier import ProviderWebhookVerifier


class StubConnectionManager:
    def verify_connectivity(self, *args, **kwargs):
        return {"verified": True}


class RemoteJobWebhookSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        self.timestamp = int(self.now.timestamp())
        self.secret = "provider-webhook-signing-secret"
        self.jobs_path = self.root / "jobs.json"
        self.webhook_state_path = self.root / "webhook-events.json"
        self.jobs = ProviderJobManager(
            self.jobs_path, clock=lambda: self.now,
            max_queue_size=8, max_concurrent=2, lease_seconds=60,
        )
        self.verifier = ProviderWebhookVerifier(
            self.webhook_state_path, clock=lambda: self.now,
        )
        self.service = ProviderService(
            StubConnectionManager(),
            job_manager=self.jobs,
            webhook_verifier=self.verifier,
        )
        self._create_remote_job()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_remote_job(self, *, provider_id="image", remote_job_id="remote-image-job-1", job_id="local-job-1"):
        accepted = self.jobs.submit(
            job_id=job_id,
            project_id="film",
            provider_id=provider_id,
            payload_ref=f"payload:{job_id}",
            idempotency_key=f"callback-key-{job_id}",
        )
        self.assertEqual(accepted["status"], "JOB_QUEUED")
        claimed = self.jobs.claim_next(worker_id=f"worker-{job_id}")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        token = claimed["job"]["lease_token"]
        begun = self.jobs.begin_remote_submission(
            job_id=job_id, worker_id=f"worker-{job_id}", lease_token=token,
        )
        self.assertEqual(begun["status"], "REMOTE_SUBMISSION_STARTED")
        submitted = self.jobs.record_remote_submission(
            job_id=job_id,
            worker_id=f"worker-{job_id}",
            lease_token=token,
            remote_job_id=remote_job_id,
            remote_status="QUEUED",
            poll_after_seconds=10,
        )
        self.assertEqual(submitted["status"], "REMOTE_JOB_SUBMITTED")
        return token

    def _signed_payload(self, event, *, timestamp=None):
        body = json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        stamp = self.timestamp if timestamp is None else timestamp
        signed = str(stamp).encode("ascii") + b"." + body
        signature = "sha256=" + hmac.new(
            self.secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        return body, signature, stamp

    def send(self, event, *, event_id="event-1", provider_id="image", signature=None):
        body, calculated_signature, stamp = self._signed_payload(event)
        return self.service.process_remote_job_webhook(
            provider_id=provider_id,
            payload=body,
            signature=signature or calculated_signature,
            timestamp=stamp,
            event_id=event_id,
            secret=self.secret,
        )

    def test_signed_completion_event_updates_only_the_matching_remote_job(self):
        payload = {
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "SUCCEEDED",
            "result_ref": "asset:final-video",
        }
        result = self.send(payload)
        self.assertTrue(result["verified"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "REMOTE_WEBHOOK_JOB_UPDATED")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["job"]["job_id"], "local-job-1")
        self.assertEqual(result["job"]["status"], "SUCCEEDED")
        self.assertEqual(result["job"]["result_ref"], "asset:final-video")
        self.assertIsNone(self.jobs.data["jobs"]["local-job-1"]["lease_token_hash"])
        self.assertIsNone(self.jobs.data["jobs"]["local-job-1"]["worker_id"])

        persisted = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["remote_job_index"]), 1)
        self.assertEqual(
            next(iter(persisted["remote_job_index"].values())),
            "local-job-1",
        )
        self.assertNotIn(self.secret, self.jobs_path.read_text(encoding="utf-8"))
        self.assertNotIn(self.secret, self.webhook_state_path.read_text(encoding="utf-8"))
        self.assertNotIn(payload.__repr__(), self.webhook_state_path.read_text(encoding="utf-8"))

    def test_replayed_event_cannot_overwrite_a_later_remote_status(self):
        running = {
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "RUNNING",
            "poll_after_seconds": 15,
        }
        first = self.send(running, event_id="provider-event-42")
        self.assertTrue(first["accepted"])
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["remote_status"], "RUNNING")

        conflicting_replay = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "FAILED",
            "error_code": "PROVIDER_FAILED",
        }, event_id="provider-event-42")
        self.assertFalse(conflicting_replay["verified"])
        self.assertEqual(conflicting_replay["status"], "WEBHOOK_REPLAY_REJECTED")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["remote_status"], "RUNNING")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["status"], "RUNNING")

    def test_provider_namespace_prevents_cross_provider_remote_id_updates(self):
        # Same opaque remote ID under another provider must not resolve to this job.
        result = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "SUCCEEDED",
            "result_ref": "asset:wrong-provider",
        }, event_id="video-event-1", provider_id="video")
        self.assertTrue(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "REMOTE_JOB_NOT_FOUND")
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_status"], "QUEUED"
        )

    def test_invalid_signature_does_not_consume_replay_key_or_update_job(self):
        payload = {
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "SUCCEEDED",
            "result_ref": "asset:should-not-save",
        }
        body, _, stamp = self._signed_payload(payload)
        result = self.service.process_remote_job_webhook(
            provider_id="image",
            payload=body,
            signature="sha256=" + "0" * 64,
            timestamp=stamp,
            event_id="invalid-signature-event",
            secret=self.secret,
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_SIGNATURE_INVALID")
        self.assertEqual(len(self.verifier.data["events"]), 0)
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_status"], "QUEUED"
        )

    def test_strict_webhook_schema_rejects_secret_fields_and_does_not_echo_them(self):
        private_value = "unexpected-provider-access-token"
        result = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "SUCCEEDED",
            "result_ref": "asset:unsafe",
            "access_token": private_value,
        }, event_id="schema-invalid-event")
        self.assertTrue(result["verified"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "WEBHOOK_EVENT_SCHEMA_INVALID")
        self.assertNotIn(private_value, repr(result))
        self.assertNotIn(private_value, self.jobs_path.read_text(encoding="utf-8"))
        self.assertNotIn(private_value, self.webhook_state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            self.jobs.data["jobs"]["local-job-1"]["remote_status"], "QUEUED"
        )

    def test_running_status_cannot_regress_to_queued_from_a_new_signed_event(self):
        first = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "RUNNING",
            "poll_after_seconds": 12,
        }, event_id="running-event")
        self.assertTrue(first["accepted"])
        second = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "QUEUED",
            "poll_after_seconds": 20,
        }, event_id="stale-queued-event")
        self.assertTrue(second["verified"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["status"], "REMOTE_JOB_STATUS_REGRESSION")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["remote_status"], "RUNNING")

    def test_terminal_state_cannot_be_overwritten_by_another_signed_terminal_event(self):
        completed = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "SUCCEEDED",
            "result_ref": "asset:correct",
        }, event_id="success-event")
        self.assertTrue(completed["accepted"])
        conflict = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "FAILED",
            "error_code": "LATE_FAILURE",
        }, event_id="late-failure-event")
        self.assertTrue(conflict["verified"])
        self.assertFalse(conflict["accepted"])
        self.assertEqual(conflict["status"], "REMOTE_JOB_TERMINAL_CONFLICT")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["status"], "SUCCEEDED")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["result_ref"], "asset:correct")

    def test_remote_job_index_is_rebuilt_for_existing_persisted_job_files(self):
        persisted = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        persisted.pop("remote_job_index")
        self.jobs_path.write_text(json.dumps(persisted), encoding="utf-8")
        restored = ProviderJobManager(
            self.jobs_path, clock=lambda: self.now,
            max_queue_size=8, max_concurrent=2, lease_seconds=60,
        )
        self.assertEqual(len(restored.data["remote_job_index"]), 1)
        result = restored.update_remote_status_from_webhook(
            provider_id="image",
            remote_job_id="remote-image-job-1",
            remote_status="SUCCEEDED",
            result_ref="asset:restored",
        )
        self.assertTrue(result["saved"])
        self.assertEqual(result["job"]["result_ref"], "asset:restored")

    def test_remote_job_identity_cannot_be_registered_to_two_local_jobs(self):
        self._create_remote_job(
            provider_id="image",
            remote_job_id="remote-image-job-1",
            job_id="local-job-2",
        )
        self.assertEqual(
            self.jobs.data["remote_job_index"],
            next(iter(self.jobs.data["remote_job_index"].items())) and self.jobs.data["remote_job_index"],
        )
        self.assertEqual(len(self.jobs.data["remote_job_index"]), 1)
        self.assertEqual(
            next(iter(self.jobs.data["remote_job_index"].values())),
            "local-job-1",
        )

    def test_unknown_or_malformed_remote_job_event_is_rejected_without_state_change(self):
        unknown = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-job-does-not-exist",
            "status": "SUCCEEDED",
            "result_ref": "asset:unknown",
        }, event_id="unknown-job-event")
        self.assertTrue(unknown["verified"])
        self.assertFalse(unknown["accepted"])
        self.assertEqual(unknown["status"], "REMOTE_JOB_NOT_FOUND")

        malformed = self.send({
            "event": "provider.job.status",
            "remote_job_id": "remote-image-job-1",
            "status": "FAILED",
            "error_code": ["not-a-code"],
        }, event_id="malformed-state-event")
        self.assertTrue(malformed["verified"])
        self.assertFalse(malformed["accepted"])
        self.assertEqual(malformed["status"], "WEBHOOK_REMOTE_JOB_STATUS_INVALID")
        self.assertEqual(self.jobs.data["jobs"]["local-job-1"]["remote_status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
