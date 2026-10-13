import hashlib
import hmac
import unittest
from concurrent.futures import ThreadPoolExecutor

from projects.provider_store.webhook_verifier import ProviderWebhookVerifier


SECRET = "a-strong-test-secret-with-more-than-32-bytes"


def signature(body, timestamp="1000", secret=SECRET):
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


class ProviderWebhookVerifierTests(unittest.TestCase):
    def setUp(self):
        self.verifier = ProviderWebhookVerifier(
            lambda provider_id: SECRET if provider_id == "image" else None,
            clock=lambda: 1000,
        )
        self.body = b'{"event":"completed"}'

    def verify(self, event_id="event-1", body=None, timestamp="1000", sig=None):
        body = self.body if body is None else body
        return self.verifier.verify(
            provider_id="image", body=body, timestamp=timestamp,
            signature=sig or signature(body, timestamp), event_id=event_id,
        )

    def test_valid_hmac_verifies_without_returning_body_or_secret(self):
        result = self.verify()
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_VERIFIED")
        self.assertNotIn(self.body.decode(), repr(result))
        self.assertNotIn(SECRET, repr(result))

    def test_tampered_body_and_signature_are_rejected(self):
        result = self.verify(body=b'{"event":"forged"}', sig=signature(self.body))
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_SIGNATURE_INVALID")
        malformed = self.verify(sig="sha256=bad")
        self.assertEqual(malformed["status"], "INVALID_WEBHOOK_SIGNATURE")

    def test_old_and_future_timestamps_are_rejected(self):
        old = self.verify(timestamp="1")
        future = self.verify(timestamp="999999")
        self.assertEqual(old["status"], "WEBHOOK_TIMESTAMP_OUT_OF_RANGE")
        self.assertEqual(future["status"], "WEBHOOK_TIMESTAMP_OUT_OF_RANGE")

    def test_event_replay_is_rejected(self):
        self.assertTrue(self.verify(event_id="same-event")["verified"])
        replay = self.verify(event_id="same-event")
        self.assertEqual(replay["status"], "WEBHOOK_REPLAY_DETECTED")

    def test_concurrent_duplicate_event_is_accepted_only_once(self):
        verifier = ProviderWebhookVerifier(lambda _: SECRET, clock=lambda: 1000)
        args = dict(
            provider_id="image", body=self.body, timestamp="1000",
            signature=signature(self.body), event_id="one-event",
        )
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: verifier.verify(**args), range(16)))
        self.assertEqual(sum(result["verified"] for result in results), 1)
        self.assertEqual(sum(result["status"] == "WEBHOOK_REPLAY_DETECTED" for result in results), 15)

    def test_body_limit_secret_lookup_and_replay_capacity_fail_closed(self):
        limited = ProviderWebhookVerifier(
            lambda _: SECRET, clock=lambda: 1000, max_body_bytes=4, max_replay_entries=1
        )
        oversized = limited.verify(
            provider_id="image", body=self.body, timestamp="1000",
            signature=signature(self.body), event_id="event-a",
        )
        self.assertEqual(oversized["status"], "WEBHOOK_BODY_TOO_LARGE")
        self.assertEqual(
            ProviderWebhookVerifier(lambda _: None, clock=lambda: 1000).verify(
                provider_id="image", body=self.body, timestamp="1000",
                signature=signature(self.body), event_id="event-a",
            )["status"],
            "WEBHOOK_SECRET_UNAVAILABLE",
        )
        short_body = b"test"
        first = limited.verify(
            provider_id="image", body=short_body, timestamp="1000",
            signature=signature(short_body), event_id="event-a",
        )
        second = limited.verify(
            provider_id="image", body=short_body, timestamp="1000",
            signature=signature(short_body), event_id="event-b",
        )
        self.assertTrue(first["verified"])
        self.assertEqual(second["status"], "WEBHOOK_REPLAY_CACHE_FULL")

    def test_invalid_inputs_and_configuration_are_rejected(self):
        self.assertEqual(
            self.verifier.verify(
                provider_id="bad id", body=self.body, timestamp="1000",
                signature=signature(self.body), event_id="event-a",
            )["status"],
            "INVALID_PROVIDER_ID",
        )
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(lambda _: SECRET, max_body_bytes=0)
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(None)


if __name__ == "__main__":
    unittest.main()
