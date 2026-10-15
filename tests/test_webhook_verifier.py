import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_service import ProviderService
from projects.provider_store.webhook_verifier import ProviderWebhookVerifier


class _Connection:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class ProviderWebhookVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.secret = "test-secret-not-persisted"
        self.verifier = ProviderWebhookVerifier(
            self.root / "replay.json",
            secret_resolver=lambda provider: self.secret if provider == "provider-a" else None,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_50_case_signature_matrix_valid_and_tampered_signatures(self):
        for index in range(1, 26):
            with self.subTest(case=index):
                timestamp = str(1_700_000_000 + index)
                body = f'{{"event":"job-{index}","value":{index}}}'.encode()
                signature = ProviderWebhookVerifier.signature_for(self.secret, timestamp, body)
                self.assertTrue(
                    ProviderWebhookVerifier.signature_matches(
                        self.secret, timestamp, body, signature
                    )
                )
                changed = ("0" if signature[0] != "0" else "1") + signature[1:]
                self.assertFalse(
                    ProviderWebhookVerifier.signature_matches(
                        self.secret, timestamp, body, changed
                    )
                )

    def test_valid_signature_is_verified_once_and_replay_is_rejected_after_reload(self):
        timestamp = "1000"
        body = b'{"event":"job.completed","job_id":"job-1"}'
        signature = ProviderWebhookVerifier.signature_for(self.secret, timestamp, body)
        result = self.verifier.verify("provider-a", body, "sha256=" + signature, timestamp, now=1000)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertTrue(result["verified"])
        reloaded = ProviderWebhookVerifier(
            self.root / "replay.json",
            secret_resolver=lambda provider: self.secret,
        )
        replay = reloaded.verify("provider-a", body, signature, timestamp, now=1000)
        self.assertEqual(replay["status"], "WEBHOOK_REPLAY")
        self.assertTrue(replay["replay"])

    def test_expired_future_and_invalid_timestamps_fail_before_secret_lookup(self):
        calls = []
        verifier = ProviderWebhookVerifier(
            self.root / "timestamps.json",
            secret_resolver=lambda provider: calls.append(provider) or self.secret,
        )
        body = b"event"
        sig = ProviderWebhookVerifier.signature_for(self.secret, "1000", body)
        self.assertEqual(verifier.verify("p", body, sig, "1", now=1000)["status"], "TIMESTAMP_EXPIRED")
        self.assertEqual(verifier.verify("p", body, sig, "2000", now=1000)["status"], "TIMESTAMP_IN_FUTURE")
        self.assertEqual(verifier.verify("p", body, sig, "1.5", now=1000)["status"], "INVALID_TIMESTAMP")
        self.assertEqual(calls, [])

    def test_bad_signature_does_not_persist_replay_record(self):
        result = self.verifier.verify("provider-a", b"event", "0" * 64, "1000", now=1000)
        self.assertEqual(result["status"], "SIGNATURE_MISMATCH")
        self.assertFalse((self.root / "replay.json").exists())

    def test_secret_lookup_failure_and_missing_secret_are_safe(self):
        broken = ProviderWebhookVerifier(
            self.root / "secret-errors.json",
            secret_resolver=lambda provider: (_ for _ in ()).throw(RuntimeError("private detail")),
        )
        result = broken.verify("provider-a", b"event", "0" * 64, "1000", now=1000)
        self.assertEqual(result["status"], "SIGNATURE_MISMATCH")
        # Signature format/age checks precede credential use; a valid signature exercises lookup.
        sig = ProviderWebhookVerifier.signature_for(self.secret, "1000", b"event")
        result = broken.verify("provider-a", b"event", sig, "1000", now=1000)
        self.assertEqual(result["status"], "SECRET_LOOKUP_FAILED")
        self.assertNotIn("private detail", repr(result))

    def test_body_size_and_signature_format_are_rejected(self):
        verifier = ProviderWebhookVerifier(
            self.root / "limits.json", secret_resolver=lambda provider: self.secret,
            max_body_bytes=4,
        )
        self.assertEqual(verifier.verify("p", b"12345", "0" * 64, "1000", now=1000)["status"], "BODY_TOO_LARGE")
        self.assertEqual(verifier.verify("p", b"x", "not-a-signature", "1000", now=1000)["status"], "INVALID_SIGNATURE_FORMAT")
        self.assertEqual(verifier.verify("", b"x", "0" * 64, "1000", now=1000)["status"], "INVALID_PROVIDER")
        self.assertEqual(verifier.verify("p", "x", "0" * 64, "1000", now=1000)["status"], "INVALID_BODY")

    def test_replay_ledger_capacity_fails_closed(self):
        verifier = ProviderWebhookVerifier(
            self.root / "capacity.json", secret_resolver=lambda provider: self.secret,
            max_replay_entries=1,
        )
        body1, body2 = b"first", b"second"
        sig1 = ProviderWebhookVerifier.signature_for(self.secret, "1000", body1)
        sig2 = ProviderWebhookVerifier.signature_for(self.secret, "1001", body2)
        self.assertEqual(verifier.verify("p", body1, sig1, "1000", now=1000)["status"], "VERIFIED")
        self.assertEqual(verifier.verify("p", body2, sig2, "1001", now=1001)["status"], "REPLAY_LEDGER_FULL")

    def test_corrupt_replay_ledger_fails_closed(self):
        path = self.root / "bad-ledger.json"
        path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(path, secret_resolver=lambda provider: self.secret)

    def test_service_integration_exposes_safe_webhook_verification(self):
        service = ProviderService(_Connection(), webhook_verifier=self.verifier)
        body = b'{"event":"job.completed"}'
        signature = ProviderWebhookVerifier.signature_for(self.secret, "1000", body)
        result = service.verify_provider_webhook(
            "provider-a", body, signature, "1000", now=1000
        )
        self.assertEqual(result["status"], "VERIFIED")
        unavailable = ProviderService(_Connection()).verify_provider_webhook(
            "provider-a", body, signature, "1000", now=1000
        )
        self.assertEqual(unavailable["status"], "WEBHOOK_VERIFIER_UNAVAILABLE")
        self.assertFalse(unavailable["verified"])

    def test_invalid_constructor_options_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderWebhookVerifier(self.root / "x.json", secret_resolver=None)
        for option, value in (
            ("max_age_seconds", 0),
            ("max_future_skew_seconds", -1),
            ("max_body_bytes", 0),
            ("max_replay_entries", 0),
        ):
            with self.subTest(option=option):
                with self.assertRaises(ValueError):
                    ProviderWebhookVerifier(
                        self.root / f"{option}.json",
                        secret_resolver=lambda provider: self.secret,
                        **{option: value},
                    )


if __name__ == "__main__":
    unittest.main()
