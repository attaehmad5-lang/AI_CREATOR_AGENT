"""Webhook signature, timestamp, replay, and secret-safety tests."""
import hashlib
import hmac
import json
from concurrent.futures import ProcessPoolExecutor
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_webhook_verifier import ProviderWebhookVerifier



def _verify_shared_webhook_event_in_process(args):
    path, now_iso, secret, payload, signature, timestamp, event_id = args
    verifier = ProviderWebhookVerifier(
        path,
        clock=lambda: datetime.fromisoformat(now_iso),
        max_payload_bytes=128,
        max_events=32,
    )
    return verifier.verify(
        provider_id="video",
        payload=payload,
        signature=signature,
        timestamp=timestamp,
        event_id=event_id,
        secret=secret,
    )["status"]


class ProviderWebhookVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "webhooks.json"
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.verifier = ProviderWebhookVerifier(
            self.path, clock=lambda: self.now, max_payload_bytes=128
        )
        self.secret = "test-webhook-secret-value"
        self.payload = b'{"event":"generation.completed","job_id":"job-1"}'
        self.timestamp = int(self.now.timestamp())

    def tearDown(self):
        self.temp_dir.cleanup()

    def signature(self, payload=None, timestamp=None):
        body = self.payload if payload is None else payload
        stamp = self.timestamp if timestamp is None else timestamp
        signed = str(stamp).encode("ascii") + b"." + body
        return "sha256=" + hmac.new(
            self.secret.encode(), signed, hashlib.sha256
        ).hexdigest()

    def test_valid_signature_is_accepted_without_persisting_secret_or_payload(self):
        result = self.verifier.verify(
            provider_id="video", payload=self.payload,
            signature=self.signature(), timestamp=self.timestamp,
            event_id="event-1", secret=self.secret,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_VERIFIED")
        state = self.path.read_text(encoding="utf-8")
        self.assertNotIn(self.secret, state)
        self.assertNotIn(self.payload.decode(), state)
        self.assertNotIn(self.secret, repr(result))

    def test_duplicate_event_is_rejected_after_valid_signature(self):
        kwargs = dict(
            provider_id="video", payload=self.payload,
            signature=self.signature(), timestamp=self.timestamp,
            event_id="event-1", secret=self.secret,
        )
        self.assertTrue(self.verifier.verify(**kwargs)["verified"])
        duplicate = self.verifier.verify(**kwargs)
        self.assertFalse(duplicate["verified"])
        self.assertEqual(duplicate["status"], "WEBHOOK_REPLAY_REJECTED")

    def test_invalid_signature_and_stale_timestamp_are_rejected(self):
        bad = self.verifier.verify(
            provider_id="video", payload=self.payload,
            signature="sha256=" + "0" * 64, timestamp=self.timestamp,
            event_id="event-bad", secret=self.secret,
        )
        self.assertEqual(bad["status"], "WEBHOOK_SIGNATURE_INVALID")
        stale_time = self.timestamp - 1000
        stale = self.verifier.verify(
            provider_id="video", payload=self.payload,
            signature=self.signature(timestamp=stale_time), timestamp=stale_time,
            event_id="event-stale", secret=self.secret,
        )
        self.assertEqual(stale["status"], "WEBHOOK_TIMESTAMP_OUT_OF_RANGE")

    def test_replay_store_capacity_rejects_new_events_without_evicting_live_keys(self):
        verifier = ProviderWebhookVerifier(
            self.path, clock=lambda: self.now, max_payload_bytes=128, max_events=1
        )
        first = verifier.verify(
            provider_id="video", payload=self.payload, signature=self.signature(),
            timestamp=self.timestamp, event_id="event-cap-1", secret=self.secret,
        )
        self.assertTrue(first["verified"])
        full = verifier.verify(
            provider_id="video", payload=self.payload, signature=self.signature(),
            timestamp=self.timestamp, event_id="event-cap-2", secret=self.secret,
        )
        self.assertEqual(full["status"], "WEBHOOK_REPLAY_STORE_FULL")
        replay = verifier.verify(
            provider_id="video", payload=self.payload, signature=self.signature(),
            timestamp=self.timestamp, event_id="event-cap-1", secret=self.secret,
        )
        self.assertEqual(replay["status"], "WEBHOOK_REPLAY_REJECTED")
        self.assertEqual(len(verifier.data["events"]), 1)

        self.now = self.now.replace(minute=11)
        next_timestamp = int(self.now.timestamp())
        next_signature = self.signature(timestamp=next_timestamp)
        accepted_after_expiry = verifier.verify(
            provider_id="video", payload=self.payload, signature=next_signature,
            timestamp=next_timestamp, event_id="event-cap-2", secret=self.secret,
        )
        self.assertTrue(accepted_after_expiry["verified"])
        self.assertEqual(len(verifier.data["events"]), 1)

    def test_replay_store_rejects_existing_state_above_configured_limit(self):
        verifier = ProviderWebhookVerifier(
            self.path, clock=lambda: self.now, max_payload_bytes=128, max_events=2
        )
        kwargs = dict(
            provider_id="video", payload=self.payload, signature=self.signature(),
            timestamp=self.timestamp, secret=self.secret,
        )
        self.assertTrue(verifier.verify(event_id="event-over-1", **kwargs)["verified"])
        self.assertTrue(verifier.verify(event_id="event-over-2", **kwargs)["verified"])
        with self.assertRaisesRegex(ValueError, "exceeds configured event limit"):
            ProviderWebhookVerifier(
                self.path, clock=lambda: self.now, max_payload_bytes=128, max_events=1
            )

    def test_oversized_payload_and_malformed_fields_fail_closed(self):
        oversized = self.verifier.verify(
            provider_id="video", payload=b"x" * 129,
            signature=self.signature(payload=b"x" * 129), timestamp=self.timestamp,
            event_id="event-big", secret=self.secret,
        )
        self.assertEqual(oversized["status"], "WEBHOOK_PAYLOAD_TOO_LARGE")
        invalid = self.verifier.verify(
            provider_id="video", payload=self.payload,
            signature=self.signature(), timestamp=True,
            event_id="event-bad-time", secret=self.secret,
        )
        self.assertEqual(invalid["status"], "INVALID_WEBHOOK_TIMESTAMP")

    def test_multi_process_replay_race_accepts_exactly_one_event(self):
        secret = self.secret
        signed = str(self.timestamp).encode("ascii") + b"." + self.payload
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        args = (
            str(self.path),
            self.now.isoformat(),
            secret,
            self.payload,
            signature,
            self.timestamp,
            "cross-process-event",
        )
        with ProcessPoolExecutor(max_workers=6) as executor:
            statuses = list(executor.map(
                _verify_shared_webhook_event_in_process,
                [args] * 6,
            ))
        self.assertEqual(statuses.count("WEBHOOK_VERIFIED"), 1)
        self.assertEqual(statuses.count("WEBHOOK_REPLAY_REJECTED"), 5)
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["events"]), 1)



if __name__ == "__main__":
    unittest.main()
