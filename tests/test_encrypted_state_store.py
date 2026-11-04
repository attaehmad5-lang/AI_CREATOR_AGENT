import base64
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.encrypted_state_store import (
    EncryptedJSONStateStore,
    EncryptedStateError,
)
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_job_manager import ProviderJobManager
from projects.provider_store.provider_tenant_isolation import ProviderTenantIsolationManager


KEY = b"0123456789abcdef0123456789abcdef"


class EncryptedJSONStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "state.json"
        self.key_provider = lambda context: KEY

    def tearDown(self):
        self.temp.cleanup()

    def store(self, *, context="test-state-v1", key_provider=None, migrate=False, max_bytes=65536):
        return EncryptedJSONStateStore(
            self.path,
            key_provider=key_provider or self.key_provider,
            context=context,
            migrate_legacy_plaintext=migrate,
            max_state_bytes=max_bytes,
        )

    def test_round_trip_hides_plaintext_and_binds_context(self):
        value = {
            "principal": "private.person@example.com",
            "credential_ref": "vault://production/provider/key",
            "nested": {"project": "documentary-01"},
        }
        store = self.store()
        store.write(value)
        raw = self.path.read_bytes()
        self.assertNotIn(b"private.person@example.com", raw)
        self.assertNotIn(b"vault://production/provider/key", raw)
        self.assertEqual(store.read({}), value)
        with self.assertRaises(EncryptedStateError):
            self.store(context="different-purpose-v1").read({})

    def test_wrong_key_and_modified_nonce_fail_authentication(self):
        self.store().write({"private": "state"})
        with self.assertRaises(EncryptedStateError):
            self.store(key_provider=lambda context: b"x" * 32).read({})

        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        envelope["nonce"] = base64.b64encode(b"z" * 12).decode("ascii")
        self.path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaises(EncryptedStateError):
            self.store().read({})

    def test_plaintext_state_requires_explicit_migration_and_is_reencrypted(self):
        legacy = {"version": "1.0", "principals": {"legacy@example.com": {"role": "creator"}}}
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaises(EncryptedStateError):
            self.store().read({})
        migrating = self.store(migrate=True)
        self.assertEqual(migrating.read({}), legacy)
        self.assertTrue(migrating.migration_required)
        migrating.write(legacy)
        self.assertFalse(migrating.migration_required)
        self.assertNotIn(b"legacy@example.com", self.path.read_bytes())
        self.assertEqual(self.store().read({}), legacy)

    def test_invalid_key_size_and_oversized_state_are_rejected(self):
        invalid = self.store(key_provider=lambda context: b"short")
        with self.assertRaises(EncryptedStateError):
            invalid.write({"safe": True})
        limited = self.store(max_bytes=1024)
        with self.assertRaises(EncryptedStateError):
            limited.write({"large": "x" * 2000})

    def test_legacy_migration_flag_requires_a_key_provider(self):
        with self.assertRaises(ValueError):
            ProviderAuthorizationManager(
                self.root / "invalid.json",
                migrate_legacy_plaintext_state=True,
            )

    def test_authorization_manager_encrypts_and_migrates_existing_identity_state(self):
        path = self.root / "authorization.json"
        legacy = ProviderAuthorizationManager(path)
        legacy.assign_role("legacy.person@example.com", "creator")
        encrypted = ProviderAuthorizationManager(
            path,
            state_encryption_key_provider=self.key_provider,
            migrate_legacy_plaintext_state=True,
        )
        raw = path.read_bytes()
        self.assertNotIn(b"legacy.person@example.com", raw)
        self.assertIn("legacy.person@example.com", encrypted.data["principals"])
        reloaded = ProviderAuthorizationManager(
            path,
            state_encryption_key_provider=self.key_provider,
        )
        self.assertIn("legacy.person@example.com", reloaded.data["principals"])

    def test_tenant_isolation_manager_encrypts_principal_and_project_ownership(self):
        path = self.root / "tenant-state.json"
        manager = ProviderTenantIsolationManager(
            path,
            state_encryption_key_provider=self.key_provider,
        )
        self.assertTrue(manager.register_tenant("tenant-01")["saved"])
        self.assertTrue(manager.bind_principal("tenant-01", "private.person@example.com")["saved"])
        self.assertTrue(manager.bind_project("tenant-01", "documentary-project")["saved"])
        raw = path.read_bytes()
        self.assertNotIn(b"private.person@example.com", raw)
        self.assertNotIn(b"documentary-project", raw)
        reloaded = ProviderTenantIsolationManager(
            path,
            state_encryption_key_provider=self.key_provider,
        )
        self.assertIn(
            "private.person@example.com",
            reloaded.data["tenants"]["tenant-01"]["principal_ids"],
        )

    def test_job_manager_encrypts_payload_and_result_references(self):
        path = self.root / "job-state.json"
        manager = ProviderJobManager(
            path,
            state_encryption_key_provider=self.key_provider,
        )
        queued = manager.submit(
            job_id="job-01",
            project_id="project-01",
            provider_id="image",
            payload_ref="private-payload-reference",
        )
        self.assertEqual(queued["status"], "JOB_QUEUED")
        raw = path.read_bytes()
        self.assertNotIn(b"private-payload-reference", raw)
        reloaded = ProviderJobManager(
            path,
            state_encryption_key_provider=self.key_provider,
        )
        self.assertEqual(
            reloaded.poll(job_id="job-01")["job"]["payload_ref"],
            "private-payload-reference",
        )


if __name__ == "__main__":
    unittest.main()
