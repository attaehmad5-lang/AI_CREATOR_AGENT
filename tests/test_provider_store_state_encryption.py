"""Deployment-key-backed encryption for sensitive provider-store state."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.budget_alert_manager import BudgetAlertManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.encrypted_state_store import EncryptedStateError
from projects.provider_store.provider_incident_manager import ProviderIncidentManager
from projects.provider_store.provider_webhook_verifier import ProviderWebhookVerifier
from projects.provider_store.quota_manager import ProviderQuotaManager

_KEY = b"privacy-state-test-key-32-bytes!!"
_FORMAT = "AI_CREATOR_AGENT_ENCRYPTED_JSON_STATE"


class ProviderStoreStateEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _key_provider(context):
        if not isinstance(context, str) or not context:
            raise AssertionError("state context must be supplied")
        return _KEY

    def test_financial_and_event_state_managers_encrypt_and_reload_state(self):
        factories = (
            ("cost", lambda path: ProviderCostPolicy(
                path, state_encryption_key_provider=self._key_provider,
            ), "ledger"),
            ("quota", lambda path: ProviderQuotaManager(
                path, limits={"image": {"daily": 10}},
                state_encryption_key_provider=self._key_provider,
            ), "data"),
            ("credit", lambda path: ProviderCreditManager(
                path, state_encryption_key_provider=self._key_provider,
            ), "data"),
            ("budget-alerts", lambda path: BudgetAlertManager(
                path, state_encryption_key_provider=self._key_provider,
            ), "data"),
            ("webhook-replay", lambda path: ProviderWebhookVerifier(
                path, state_encryption_key_provider=self._key_provider,
            ), "data"),
            ("incidents", lambda path: ProviderIncidentManager(
                path, state_encryption_key_provider=self._key_provider,
            ), "data"),
        )
        marker = "state-must-not-appear-in-plaintext"

        for name, factory, attribute in factories:
            with self.subTest(manager=name):
                path = self.root / f"{name}.json"
                manager = factory(path)
                state = getattr(manager, attribute)
                state["privacy_test_marker"] = marker
                manager._save()

                raw = path.read_text(encoding="utf-8")
                self.assertIn(_FORMAT, raw)
                self.assertNotIn(marker, raw)
                restored = factory(path)
                restored_state = getattr(restored, attribute)
                self.assertEqual(restored_state["privacy_test_marker"], marker)

    def test_encrypted_managers_reject_plaintext_without_explicit_migration(self):
        path = self.root / "legacy-webhook.json"
        legacy = {"version": "1.0", "events": {}}
        path.write_text(json.dumps(legacy), encoding="utf-8")

        with self.assertRaises(EncryptedStateError):
            ProviderWebhookVerifier(
                path,
                state_encryption_key_provider=self._key_provider,
            )

    def test_explicit_legacy_migration_rewrites_state_as_ciphertext(self):
        path = self.root / "migrate-webhook.json"
        legacy = {"version": "1.0", "events": {}}
        path.write_text(json.dumps(legacy), encoding="utf-8")

        manager = ProviderWebhookVerifier(
            path,
            state_encryption_key_provider=self._key_provider,
            migrate_legacy_plaintext_state=True,
        )

        raw = path.read_text(encoding="utf-8")
        self.assertIn(_FORMAT, raw)
        self.assertNotIn('"events"', raw)
        self.assertEqual(manager.data, legacy)

    def test_migration_cannot_be_enabled_without_key_provider(self):
        with self.assertRaises(ValueError):
            ProviderIncidentManager(
                self.root / "invalid-migration.json",
                migrate_legacy_plaintext_state=True,
            )


if __name__ == "__main__":
    unittest.main()
