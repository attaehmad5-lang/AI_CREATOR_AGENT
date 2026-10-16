import unittest

from projects.provider_store.provider_service import ProviderService
from projects.provider_store.webhook_verifier import ProviderWebhookVerifier


class _ConnectionManager:
    def verify_connectivity(self, provider_id, probe, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class ProviderServiceWebhookTests(unittest.TestCase):
    def setUp(self):
        self.service = ProviderService(_ConnectionManager())
        self.secret = "service-webhook-secret-with-entropy"
        self.verifier = ProviderWebhookVerifier(self.secret)
        self.body = b'{"event":"generation.completed"}'
        self.timestamp = 1_800_000_000
        self.signature = self.verifier.sign_for_testing(self.secret, self.body, self.timestamp)

    def test_service_returns_only_safe_verified_event_metadata(self):
        result = self.service.verify_webhook_callback(
            self.verifier,
            self.body,
            signature_header=self.signature,
            timestamp_header=str(self.timestamp),
            event_id="evt_service_1",
            now=self.timestamp,
        )
        self.assertEqual(result, {
            "status": "VERIFIED", "verified": True, "event_id": "evt_service_1",
        })
        self.assertNotIn(self.secret, repr(result))
        self.assertNotIn(self.body.decode(), repr(result))

    def test_service_fails_closed_on_invalid_signature_or_missing_verifier(self):
        invalid = self.service.verify_webhook_callback(
            self.verifier,
            self.body,
            signature_header="sha256=" + "0" * 64,
            timestamp_header=str(self.timestamp),
            event_id="evt_invalid",
            now=self.timestamp,
        )
        self.assertEqual(invalid, {"status": "SIGNATURE_MISMATCH", "verified": False})
        missing = self.service.verify_webhook_callback(
            object(), self.body, signature_header=self.signature,
            timestamp_header=str(self.timestamp), event_id="evt_invalid", now=self.timestamp,
        )
        self.assertEqual(missing, {"status": "WEBHOOK_VERIFIER_UNAVAILABLE", "verified": False})
        class ExplodingVerifier:
            @property
            def verify(self):
                raise RuntimeError("private verifier lookup detail")
        exploding = self.service.verify_webhook_callback(
            ExplodingVerifier(), self.body, signature_header=self.signature,
            timestamp_header=str(self.timestamp), event_id="evt_invalid", now=self.timestamp,
        )
        self.assertEqual(exploding["status"], "WEBHOOK_VERIFIER_LOOKUP_FAILED")
        self.assertEqual(exploding["error_type"], "RuntimeError")
        self.assertNotIn("private verifier lookup detail", repr(exploding))

    def test_service_rejects_malformed_verifier_results(self):
        class InvalidVerifier:
            def verify(self, *args, **kwargs):
                return ["valid"]
        result = self.service.verify_webhook_callback(
            InvalidVerifier(), self.body, signature_header=self.signature,
            timestamp_header=str(self.timestamp), event_id="evt_invalid", now=self.timestamp,
        )
        self.assertEqual(result, {
            "status": "WEBHOOK_VERIFIER_INVALID_RESULT", "verified": False,
        })


if __name__ == "__main__":
    unittest.main()
