"""Webhook signature, timestamp, replay, and secret-safety tests."""
import hashlib
import hmac
import tempfile
from concurrent.futures import ProcessPoolExecutor
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_webhook_verifier import ProviderWebhookVerifier


def _verify_same_webhook_in_process(args):
    path, timestamp, secret, payload, signature, event_id = args
    now = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    verifier = ProviderWebhookVerifier(path, clock=lambda: now)
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

    def test_same_signed_event_is_accepted_once_across_processes(self):
        payload = b'{"event":"generation.completed","job_id":"shared-job"}'
        secret = "shared-process-webhook-secret"
        timestamp = self.timestamp
        signed = str(timestamp).encode("ascii") + b"." + payload
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        with ProcessPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(
                _verify_same_webhook_in_process,
                [
                    (str(self.path), timestamp, secret, payload, signature, "one-event")
                    for _ in range(18)
                ],
            ))
        self.assertEqual(statuses.count("WEBHOOK_VERIFIED"), 1)
        self.assertEqual(statuses.count("WEBHOOK_REPLAY_REJECTED"), 17)
        reloaded = ProviderWebhookVerifier(self.path, clock=lambda: self.now)
        self.assertEqual(len(reloaded.data["events"]), 1)

    def test_replay_state_rejects_duplicate_keys_and_symlinks(self):
        self.path.write_text(
            '{"version":"1.0","events":{},"events":{"injected":{}}}',
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(self.path)
        target = self.path.parent / "real-webhook-state.json"
        target.write_text('{"version":"1.0","events":{}}', encoding="utf-8")
        link = self.path.parent / "linked-webhook-state.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(link)

    def test_webhook_clock_and_unicode_secret_controls_fail_closed(self):
        invalid_clock = ProviderWebhookVerifier(
            self.path, clock=lambda: datetime(2026, 10, 16, 12)
        )
        result = invalid_clock.verify(
            provider_id="video", payload=self.payload,
            signature=self.signature(), timestamp=self.timestamp,
            event_id="event-clock", secret=self.secret,
        )
        self.assertEqual(result["status"], "WEBHOOK_CLOCK_INVALID")
        for control in (chr(0x0085), chr(0x2028), chr(0x2029)):
            with self.subTest(control=control):
                result = self.verifier.verify(
                    provider_id="video", payload=self.payload,
                    signature=self.signature(), timestamp=self.timestamp,
                    event_id="event-secret-" + str(ord(control)),
                    secret=self.secret + control,
                )
                self.assertEqual(result["status"], "WEBHOOK_SECRET_INVALID")

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


if __name__ == "__main__":
    unittest.main()
