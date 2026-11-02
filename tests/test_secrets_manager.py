import threading
import unittest
from datetime import datetime, timedelta, timezone

from projects.provider_store.secrets_manager import (
    EnvironmentSecretBackend,
    ProviderSecretsManager,
    SecretLease,
    SecretManagerError,
)


class FakeVaultBackend:
    def __init__(self):
        self.values = {"vault://provider/credential": "vault-secret-a"}
        self.reads = 0
        self.rotations = []
        self.failure = None

    def resolve_secret(self, reference, **kwargs):
        self.reads += 1
        if self.failure:
            raise self.failure
        value = self.values.get(reference)
        if value is None:
            raise SecretManagerError("SECRET_NOT_FOUND")
        return {"value": value, "version_id": f"version-{self.reads}"}

    def rotate_secret(self, reference, **kwargs):
        self.rotations.append((reference, dict(kwargs)))
        self.values[reference] = f"vault-secret-{len(self.rotations) + 1}"
        return {
            "rotated": True,
            "version_id": f"rotated-{len(self.rotations)}",
            "rotated_at": "2026-11-02T12:00:00+00:00",
        }


class AllowRotation:
    def authorize(self, principal_id, operation):
        return {
            "allowed": principal_id == "admin-1" and operation == "provider.credentials.rotate",
            "status": "AUTHORIZED",
        }


class DenyTenantCredential:
    def __init__(self, allowed=False):
        self.allowed = allowed
        self.calls = []

    def authorize_credential(self, tenant_id, reference, principal_id):
        self.calls.append((tenant_id, reference, principal_id))
        return {"allowed": self.allowed, "status": "TENANT_ACCESS_AUTHORIZED" if self.allowed else "TENANT_CREDENTIAL_ACCESS_DENIED"}


class ProviderSecretsManagerTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVaultBackend()
        self.manager = ProviderSecretsManager(
            backends={"vault": self.vault},
            environ={"HF_TOKEN": "environment-secret"},
        )

    def test_environment_secret_is_resolved_into_a_redacted_lease(self):
        lease = self.manager.resolve_secret(
            "env://HF_TOKEN", provider_id="huggingface", purpose="provider_execution"
        )
        self.assertIsInstance(lease, SecretLease)
        self.assertEqual(lease.reveal(), "environment-secret")
        self.assertEqual(lease.backend, "env")
        self.assertNotIn("environment-secret", repr(lease))
        self.assertNotIn("environment-secret", str(lease))
        lease.close()
        self.assertTrue(lease.closed)
        self.assertEqual(repr(lease), "<SecretLease redacted>")
        with self.assertRaisesRegex(SecretManagerError, "SECRET_LEASE_CLOSED"):
            lease.reveal()

    def test_external_backend_secret_is_resolved_without_persistent_cache(self):
        first = self.manager.resolve_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            purpose="connectivity_probe",
        )
        self.assertEqual(first.reveal(), "vault-secret-a")
        first.close()
        self.vault.values["vault://provider/credential"] = "vault-secret-b"
        second = self.manager.resolve_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            purpose="provider_execution",
        )
        self.assertEqual(second.reveal(), "vault-secret-b")
        self.assertEqual(self.vault.reads, 2)
        second.close()

    def test_reference_inspection_never_reads_the_secret(self):
        before = self.vault.reads
        self.assertEqual(
            self.manager.inspect_reference("vault://provider/credential", provider_id="huggingface"),
            {"valid": True, "status": "SECRET_BACKEND_READY", "scheme": "vault"},
        )
        self.assertEqual(self.vault.reads, before)

    def test_unknown_backend_fails_closed(self):
        self.assertEqual(
            self.manager.inspect_reference("aws-sm://account/provider/key")["status"],
            "SECRET_BACKEND_UNAVAILABLE",
        )
        with self.assertRaisesRegex(SecretManagerError, "SECRET_BACKEND_UNAVAILABLE"):
            self.manager.resolve_secret(
                "aws-sm://account/provider/key",
                provider_id="huggingface",
                purpose="provider_execution",
            )

    def test_invalid_and_ambiguous_references_are_rejected(self):
        for reference in (
            "vault/provider/credential",
            "vault://../credential",
            "vault://provider/../credential",
            "vault://provider//credential",
            "vault://provider/credential?version=latest",
            "vault://provider/credential#key",
            "vault://user:password@provider/credential",
            "env://lowercase",
            "env://HF_TOKEN/extra",
            "env://HF_TOKEN\nMALICIOUS",
            "vault://provider\\credential",
        ):
            with self.subTest(reference=reference):
                self.assertFalse(self.manager.inspect_reference(reference)["valid"])
                with self.assertRaises(SecretManagerError):
                    self.manager.resolve_secret(
                        reference, provider_id="huggingface", purpose="provider_execution"
                    )

    def test_custom_backend_registration_is_validated(self):
        self.assertEqual(
            self.manager.register_backend("aws-sm", object())["status"],
            "INVALID_SECRET_BACKEND",
        )
        self.assertEqual(
            self.manager.register_backend("env", FakeVaultBackend())["status"],
            "INVALID_SECRET_BACKEND_SCHEME",
        )
        self.assertEqual(
            self.manager.register_backend("aws-sm", self.vault)["status"],
            "SECRET_BACKEND_REGISTERED",
        )
        self.assertEqual(
            self.manager.inspect_reference("aws-sm://account/provider/key")["status"],
            "SECRET_BACKEND_READY",
        )

    def test_secret_length_and_control_characters_are_rejected(self):
        manager = ProviderSecretsManager(
            backends={"vault": self.vault}, max_secret_bytes=8
        )
        with self.assertRaisesRegex(SecretManagerError, "INVALID_SECRET_VALUE"):
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )
        self.vault.values["vault://provider/credential"] = "bad\nsecret"
        with self.assertRaisesRegex(SecretManagerError, "INVALID_SECRET_VALUE"):
            self.manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )

    def test_expired_backend_secret_is_rejected(self):
        class ExpiredBackend:
            def resolve_secret(self, reference, **kwargs):
                return {
                    "value": "expired-secret",
                    "version_id": "v1",
                    "expires_at": "2000-01-01T00:00:00+00:00",
                }

        manager = ProviderSecretsManager(backends={"vault": ExpiredBackend()})
        with self.assertRaisesRegex(SecretManagerError, "SECRET_EXPIRED"):
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )

    def test_naive_or_malformed_expiry_metadata_is_rejected(self):
        class BadMetadataBackend:
            def __init__(self, expires_at):
                self.expires_at = expires_at

            def resolve_secret(self, reference, **kwargs):
                return {"value": "short-secret", "expires_at": self.expires_at}

        for expires_at in ("not-a-date", "2026-11-03T00:00:00"):
            manager = ProviderSecretsManager(
                backends={"vault": BadMetadataBackend(expires_at)}
            )
            with self.subTest(expires_at=expires_at):
                with self.assertRaisesRegex(SecretManagerError, "INVALID_SECRET_BACKEND_METADATA"):
                    manager.resolve_secret(
                        "vault://provider/credential",
                        provider_id="huggingface",
                        purpose="provider_execution",
                    )

    def test_unknown_backend_response_fields_are_rejected(self):
        class UnsafeBackend:
            def resolve_secret(self, reference, **kwargs):
                return {"value": "short-secret", "debug": "private"}

        manager = ProviderSecretsManager(backends={"vault": UnsafeBackend()})
        with self.assertRaisesRegex(SecretManagerError, "INVALID_SECRET_BACKEND_RESPONSE"):
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )

    def test_backend_exception_text_is_not_reflected(self):
        class RaisingBackend:
            def resolve_secret(self, reference, **kwargs):
                raise RuntimeError("vault response token=DO_NOT_LEAK")

        manager = ProviderSecretsManager(backends={"vault": RaisingBackend()})
        with self.assertRaisesRegex(SecretManagerError, "SECRET_BACKEND_FAILED") as error:
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )
        self.assertNotIn("DO_NOT_LEAK", repr(error.exception))
        self.assertNotIn("vault response", str(error.exception))

    def test_invalid_provider_purpose_and_version_are_rejected(self):
        for kwargs, expected in (
            ({"provider_id": "", "purpose": "provider_execution"}, "INVALID_PROVIDER_ID"),
            ({"provider_id": "huggingface", "purpose": "other"}, "INVALID_SECRET_PURPOSE"),
            ({"provider_id": "huggingface", "purpose": "provider_execution", "version_id": "x" * 129}, "INVALID_SECRET_VERSION"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(SecretManagerError, expected):
                    self.manager.resolve_secret(
                        "vault://provider/credential", **kwargs
                    )

    def test_rotation_requires_confirmation_and_authorization(self):
        no_auth = ProviderSecretsManager(backends={"vault": self.vault})
        missing = no_auth.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
        )
        self.assertEqual(missing["status"], "AUTHORIZATION_MANAGER_REQUIRED")

        manager = ProviderSecretsManager(
            backends={"vault": self.vault}, authorization_manager=AllowRotation()
        )
        not_confirmed = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=False,
        )
        self.assertEqual(not_confirmed["status"], "ROTATION_CONFIRMATION_REQUIRED")
        denied = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="viewer-1",
            confirmed=True,
        )
        self.assertEqual(denied["status"], "ROTATION_ACCESS_DENIED")
        self.assertEqual(self.vault.rotations, [])

    def test_rotation_uses_idempotency_key_and_returns_only_safe_metadata(self):
        manager = ProviderSecretsManager(
            backends={"vault": self.vault}, authorization_manager=AllowRotation()
        )
        result = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
        )
        self.assertEqual(result["status"], "SECRET_ROTATED")
        self.assertTrue(result["rotated"])
        self.assertEqual(result["version_id"], "rotated-1")
        self.assertNotIn("vault-secret", repr(result))
        self.assertEqual(self.vault.rotations[0][1]["idempotency_key"], "rotation-0001")

    def test_rotation_requires_valid_idempotency_reference_and_backend_capability(self):
        for key in ("x", "", "bad key", "rotation"):
            result = self.manager.rotate_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                idempotency_key=key,
                principal_id="admin-1",
                confirmed=True,
            )
            self.assertEqual(result["status"], "INVALID_IDEMPOTENCY_KEY")
        env_result = ProviderSecretsManager(
            authorization_manager=AllowRotation(),
            environ={"HF_TOKEN": "environment-secret"},
        ).rotate_secret(
            "env://HF_TOKEN",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
        )
        self.assertEqual(env_result["status"], "SECRET_ROTATION_UNSUPPORTED")

    def test_rotation_backend_response_cannot_expose_secret_or_malformed_metadata(self):
        class UnsafeRotationBackend:
            def resolve_secret(self, reference, **kwargs):
                return "secret"

            def rotate_secret(self, reference, **kwargs):
                return {"rotated": True, "secret": "must-not-leak"}

        manager = ProviderSecretsManager(
            backends={"vault": UnsafeRotationBackend()},
            authorization_manager=AllowRotation(),
        )
        result = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
        )
        self.assertEqual(result["status"], "INVALID_ROTATION_RESPONSE")
        self.assertNotIn("must-not-leak", repr(result))

    def test_tenant_isolation_is_required_and_checked_before_backend_read(self):
        isolation = DenyTenantCredential(allowed=False)
        manager = ProviderSecretsManager(
            backends={"vault": self.vault}, tenant_isolation_manager=isolation
        )
        with self.assertRaisesRegex(SecretManagerError, "TENANT_CONTEXT_REQUIRED"):
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
            )
        with self.assertRaisesRegex(SecretManagerError, "TENANT_CREDENTIAL_ACCESS_DENIED"):
            manager.resolve_secret(
                "vault://provider/credential",
                provider_id="huggingface",
                purpose="provider_execution",
                tenant_id="tenant-a",
                principal_id="user-a",
            )
        self.assertEqual(self.vault.reads, 0)
        self.assertEqual(len(isolation.calls), 1)

    def test_tenant_authorization_and_rotation_are_bound_to_the_same_reference(self):
        isolation = DenyTenantCredential(allowed=True)
        manager = ProviderSecretsManager(
            backends={"vault": self.vault},
            tenant_isolation_manager=isolation,
            authorization_manager=AllowRotation(),
        )
        lease = manager.resolve_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            purpose="provider_execution",
            tenant_id="tenant-a",
            principal_id="admin-1",
        )
        self.assertEqual(lease.reveal(), "vault-secret-a")
        lease.close()
        rotated = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
            tenant_id="tenant-a",
        )
        self.assertEqual(rotated["status"], "SECRET_ROTATED")
        self.assertEqual(
            isolation.calls,
            [
                ("tenant-a", "vault://provider/credential", "admin-1"),
                ("tenant-a", "vault://provider/credential", "admin-1"),
            ],
        )

    def test_backend_reads_can_run_concurrently_without_shared_secret_cache(self):
        manager = ProviderSecretsManager(
            backends={"vault": self.vault}
        )
        results = []
        errors = []

        def worker():
            try:
                lease = manager.resolve_secret(
                    "vault://provider/credential",
                    provider_id="huggingface",
                    purpose="provider_execution",
                )
                results.append(lease)
            except Exception as exc:
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(self.vault.reads, 8)
        for lease in results:
            lease.close()

    def test_malformed_rotation_metadata_is_not_returned(self):
        class BadRotateBackend:
            def resolve_secret(self, reference, **kwargs):
                return "secret"

            def rotate_secret(self, reference, **kwargs):
                return {"rotated": True, "rotated_at": "not-a-date"}

        manager = ProviderSecretsManager(
            backends={"vault": BadRotateBackend()},
            authorization_manager=AllowRotation(),
        )
        result = manager.rotate_secret(
            "vault://provider/credential",
            provider_id="huggingface",
            idempotency_key="rotation-0001",
            principal_id="admin-1",
            confirmed=True,
        )
        self.assertEqual(result["status"], "INVALID_ROTATION_METADATA")


if __name__ == "__main__":
    unittest.main()
