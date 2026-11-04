"""Encryption regressions for operational provider state managers."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.encrypted_state_store import EncryptedStateError
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.project_policy_manager import ProviderProjectPolicyManager
from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager
from projects.provider_store.provider_feature_flags import ProviderFeatureFlagManager
from projects.provider_store.provider_slo_manager import ProviderSLOManager

_KEY = b"operational-state-key-32-bytes!!"


class OperationalProviderStateEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _key_provider(context):
        if not isinstance(context, str) or not context:
            raise AssertionError("stable state context must be provided")
        return _KEY

    def _factories(self):
        return (
            ("health", lambda path, **kw: ProviderHealthManager(path, **kw)),
            ("feature-flags", lambda path, **kw: ProviderFeatureFlagManager(path, **kw)),
            ("degraded-mode", lambda path, **kw: ProviderDegradedModeManager(path, **kw)),
            ("slo", lambda path, **kw: ProviderSLOManager(path, **kw)),
            ("project-policy", lambda path, **kw: ProviderProjectPolicyManager(path, **kw)),
        )

    def test_operational_state_files_encrypt_and_reload_without_plaintext(self):
        marker = "operational-state-private-marker"
        for name, factory in self._factories():
            with self.subTest(manager=name):
                path = self.root / f"{name}.json"
                options = {"state_encryption_key_provider": self._key_provider}
                manager = factory(path, **options)
                manager.data["privacy_test_marker"] = marker
                manager._save() if name != "project-policy" else manager._save_unlocked()

                raw = path.read_text(encoding="utf-8")
                self.assertIn("AI_CREATOR_AGENT_ENCRYPTED_JSON_STATE", raw)
                self.assertNotIn(marker, raw)

                restored = factory(path, **options)
                self.assertEqual(restored.data["privacy_test_marker"], marker)

    def test_encryption_rejects_plaintext_state_without_explicit_migration(self):
        defaults = {
            "health": {"version": "1.0", "providers": {}},
            "feature-flags": {"version": "1.0", "flags": {}},
            "degraded-mode": {"version": "1.0", "policies": {}, "states": {}},
            "slo": {"version": "1.0", "objectives": {}, "samples": []},
            "project-policy": {"version": "1.0", "projects": {}},
        }
        for name, factory in self._factories():
            with self.subTest(manager=name):
                path = self.root / f"legacy-{name}.json"
                path.write_text(json.dumps(defaults[name]), encoding="utf-8")
                with self.assertRaises(EncryptedStateError):
                    factory(path, state_encryption_key_provider=self._key_provider)

    def test_explicit_legacy_migration_rewrites_operational_state_as_ciphertext(self):
        path = self.root / "legacy-operational-state.json"
        legacy = {"version": "1.0", "providers": {}}
        path.write_text(json.dumps(legacy), encoding="utf-8")

        manager = ProviderHealthManager(
            path,
            state_encryption_key_provider=self._key_provider,
            migrate_legacy_plaintext_state=True,
        )

        raw = path.read_text(encoding="utf-8")
        self.assertIn("AI_CREATOR_AGENT_ENCRYPTED_JSON_STATE", raw)
        self.assertNotIn('"providers"', raw)
        self.assertEqual(manager.data, legacy)

    def test_migration_cannot_be_enabled_without_key_provider(self):
        for _, factory in self._factories():
            with self.assertRaises(ValueError):
                factory(
                    self.root / "must-not-create-plaintext.json",
                    migrate_legacy_plaintext_state=True,
                )


if __name__ == "__main__":
    unittest.main()
