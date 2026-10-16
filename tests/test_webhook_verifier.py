import unittest

from projects.provider_store.webhook_verifier import ProviderWebhookVerifier


class ProviderWebhookVerifierTests(unittest.TestCase):
    def setUp(self):
        self.secret = "test-webhook-secret-with-enough-entropy"
        self.verifier = ProviderWebhookVerifier(self.secret, max_age_seconds=300)
        self.body = b'{"id":"evt_1","type":"generation.completed"}'
        self.timestamp = 1_800_000_000

    def _verify(self, event_id="evt_1", body=None, timestamp=None, signature=None, now=None):
        ts = self.timestamp if timestamp is None else timestamp
        payload = self.body if body is None else body
        signed = signature or self.verifier.sign_for_testing(self.secret, payload, ts)
        return self.verifier.verify(
            payload,
            signature_header=signed,
            timestamp_header=str(ts),
            event_id=event_id,
            now=float(ts if now is None else now),
        )

    def test_valid_signature_is_accepted_once(self):
        result = self._verify()
        self.assertEqual(result, {"valid": True, "status": "VERIFIED", "event_id": "evt_1"})
        self.assertEqual(self.verifier.replay_cache_size(), 1)

    def test_tampered_body_and_signature_are_rejected(self):
        signature = self.verifier.sign_for_testing(self.secret, self.body, self.timestamp)
        result = self._verify(body=self.body + b" ", signature=signature)
        self.assertEqual(result["status"], "SIGNATURE_MISMATCH")
        self.assertEqual(self.verifier.replay_cache_size(), 0)

    def test_stale_and_future_timestamps_are_rejected(self):
        stale = self._verify(timestamp=self.timestamp - 301, now=self.timestamp)
        future = self._verify(event_id="evt_future", timestamp=self.timestamp + 301, now=self.timestamp)
        self.assertEqual(stale["status"], "TIMESTAMP_OUT_OF_WINDOW")
        self.assertEqual(future["status"], "TIMESTAMP_OUT_OF_WINDOW")

    def test_replayed_event_id_is_rejected_after_valid_signature(self):
        self.assertTrue(self._verify()["valid"])
        replay = self._verify()
        self.assertFalse(replay["valid"])
        self.assertEqual(replay["status"], "REPLAY_DETECTED")

    def test_invalid_inputs_and_oversized_payload_fail_closed(self):
        self.assertEqual(self.verifier.verify(
            "not-bytes", signature_header="sha256=" + "0" * 64,
            timestamp_header=str(self.timestamp), event_id="evt", now=self.timestamp,
        )["status"], "INVALID_BODY")
        self.assertEqual(self.verifier.verify(
            b"x" * (self.verifier.max_body_bytes + 1),
            signature_header="sha256=" + "0" * 64,
            timestamp_header=str(self.timestamp), event_id="evt", now=self.timestamp,
        )["status"], "BODY_TOO_LARGE")
        self.assertEqual(self._verify(signature="invalid")["status"], "INVALID_SIGNATURE_FORMAT")
        bad_time = self.verifier.verify(
            self.body, signature_header="sha256=" + "0" * 64,
            timestamp_header="1e3", event_id="evt", now=self.timestamp,
        )
        self.assertEqual(bad_time["status"], "INVALID_TIMESTAMP")
        self.assertEqual(self._verify(event_id="../unsafe")["status"], "INVALID_EVENT_ID")
        for invalid_clock in (float("nan"), float("inf"), float("-inf")):
            result = self.verifier.verify(
                self.body,
                signature_header=self.verifier.sign_for_testing(self.secret, self.body, self.timestamp),
                timestamp_header=str(self.timestamp),
                event_id="evt_clock",
                now=invalid_clock,
            )
            self.assertEqual(result["status"], "INVALID_CLOCK")

    def test_replay_cache_is_bounded(self):
        verifier = ProviderWebhookVerifier(self.secret, max_replay_entries=2)
        for event_id in ("evt_1", "evt_2", "evt_3"):
            signature = verifier.sign_for_testing(self.secret, self.body, self.timestamp)
            result = verifier.verify(
                self.body, signature_header=signature, timestamp_header=str(self.timestamp),
                event_id=event_id, now=self.timestamp,
            )
            self.assertTrue(result["valid"])
        self.assertEqual(verifier.replay_cache_size(), 2)

    def test_secret_and_limits_are_validated(self):
        for secret in ("short", "invalid\nsecret"):
            with self.subTest(secret=repr(secret)), self.assertRaises(ValueError):
                ProviderWebhookVerifier(secret)
        for kwargs in (
            {"max_age_seconds": 0},
            {"max_body_bytes": True},
            {"max_replay_entries": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProviderWebhookVerifier(self.secret, **kwargs)

    def test_concurrent_replay_attempts_accept_only_one(self):
        from concurrent.futures import ThreadPoolExecutor

        verifier = ProviderWebhookVerifier(self.secret)
        signature = verifier.sign_for_testing(self.secret, self.body, self.timestamp)
        def attempt(_):
            return verifier.verify(
                self.body, signature_header=signature, timestamp_header=str(self.timestamp),
                event_id="same-event", now=self.timestamp,
            )
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(40)))
        self.assertEqual(sum(result["valid"] for result in results), 1)
        self.assertEqual(sum(result["status"] == "REPLAY_DETECTED" for result in results), 39)


if __name__ == "__main__":
    unittest.main()
