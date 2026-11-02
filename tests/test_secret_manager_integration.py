import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.http_probe import ProviderHTTPConnectivityProbe
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.secrets_manager import (
    ProviderSecretsManager,
    SecretLease,
    SecretManagerError,
)


class FakeVaultBackend:
    def __init__(self):
        self.value = "integration-secret-value"
        self.reads = 0
        self.rotations = []

    def resolve_secret(self, reference, **kwargs):
        self.reads += 1
        return {"value": self.value, "version_id": f"v{self.reads}"}

    def rotate_secret(self, reference, **kwargs):
        self.rotations.append(dict(kwargs))
        self.value = f"rotated-secret-{len(self.rotations)}"
        return {
            "rotated": True,
            "version_id": f"v{len(self.rotations) + 1}",
            "rotated_at": "2026-11-02T12:00:00+00:00",
        }


class FakeResponse:
    status = 200

    def __init__(self):
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return b'{"ok":true}'[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self):
        self.response = FakeResponse()
        self.calls = []
        self.request_headers = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        self.request_headers.append({
            key.casefold(): value for key, value in request.header_items()
        })
        return self.response


class StubRoutingManager:
    def route(self, **kwargs):
        return {"allowed": True, "provider": "managed-image", "billing_mode": "FREE"}


class StubConnectionManager:
    def __init__(self, provider):
        self.provider = dict(provider)

    def verify_connectivity(self, *args, **kwargs):
        return {"verified": True, "status": "CONNECTED"}

    def get_provider(self, provider_id):
        return dict(self.provider) if provider_id == self.provider["id"] else None


class ProviderSecretsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = FakeVaultBackend()
        self.auth = ProviderAuthorizationManager(self.root / "auth.json")
        self.auth.assign_role("admin-1", "admin")
        self.auth.assign_role("viewer-1", "viewer")
        self.secrets = ProviderSecretsManager(
            backends={"vault": self.backend},
            authorization_manager=self.auth,
        )
        self.provider = {
            "id": "managed-image",
            "name": "Managed Image",
            "auth_type": "API_KEY",
            "secret_ref": "vault://provider/account-key",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_connection_manager_reports_managed_reference_without_reading_secret(self):
        catalog = self.root / "catalog.json"
        state = self.root / "state.json"
        catalog.write_text(json.dumps({"providers": [{
            **self.provider, "category": "image", "connection_type": "API",
            "enabled": True, "allowed_hosts": ["api.example.com"],
        }]}), encoding="utf-8")
        state.write_text(json.dumps({
            "version": "1.0", "policy": "USER_CONTROLLED", "providers": {},
        }), encoding="utf-8")
        manager = ConnectionManager(catalog, state, secret_manager=self.secrets)
        status = manager.credential_status("managed-image")
        self.assertEqual(status["status"], "CREDENTIALS_PRESENT")
        self.assertEqual(status["credential_source"], "vault")
        self.assertNotIn(self.backend.value, repr(status))
        self.assertEqual(self.backend.reads, 0)
        verified = manager.verify_connectivity(
            "managed-image", lambda provider, **kwargs: {"ok": True}
        )
        self.assertTrue(verified["verified"])
        self.assertEqual(self.backend.reads, 0)
        self.assertNotIn(self.backend.value, state.read_text(encoding="utf-8"))
        self.assertNotIn(self.backend.value, repr(verified))

    def test_connection_manager_blocks_managed_reference_without_secret_backend(self):
        catalog = self.root / "catalog-no-backend.json"
        state = self.root / "state-no-backend.json"
        catalog.write_text(json.dumps({"providers": [{
            **self.provider, "category": "image", "connection_type": "API",
            "enabled": True, "allowed_hosts": ["api.example.com"],
        }]}), encoding="utf-8")
        state.write_text(json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}), encoding="utf-8")
        manager = ConnectionManager(catalog, state)
        result = manager.verify_connectivity(
            "managed-image", lambda provider, **kwargs: self.fail("probe must be blocked")
        )
        self.assertEqual(result["status"], "SECRET_MANAGER_REQUIRED")
        self.assertFalse(result["verified"])

    def test_http_probe_uses_ephemeral_managed_secret_and_closes_lease(self):
        original_resolve = self.secrets.resolve_secret
        captured = {}

        def tracking_resolve(*args, **kwargs):
            lease = original_resolve(*args, **kwargs)
            captured["lease"] = lease
            return lease

        self.secrets.resolve_secret = tracking_resolve
        opener = FakeOpener()
        probe = ProviderHTTPConnectivityProbe(
            secret_manager=self.secrets,
            opener=opener,
            principal_id="admin-1",
        )
        result = probe({
            **self.provider,
            "allowed_hosts": ["api.example.com"],
            "configuration": {
                "endpoint": "https://api.example.com/health",
                "authentication_required": True,
            },
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["authentication_verified"])
        request = opener.calls[0][0]
        self.assertEqual(
            opener.request_headers[0]["authorization"], "Bearer integration-secret-value"
        )
        self.assertTrue(captured["lease"].closed)
        self.assertNotIn("integration-secret-value", repr(result))
        self.assertTrue(opener.response.closed)
        self.assertNotIn("authorization", {k.casefold(): v for k, v in request.header_items()})

    def test_http_probe_fails_before_network_when_managed_secret_manager_is_missing(self):
        opener = FakeOpener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaisesRegex(PermissionError, "managed provider credential is unavailable"):
            probe({
                **self.provider,
                "allowed_hosts": ["api.example.com"],
                "configuration": {
                    "endpoint": "https://api.example.com/health",
                    "authentication_required": True,
                },
            })
        self.assertEqual(opener.calls, [])

    def _service(self, *, provider=None, secret_manager=None):
        connection_manager = StubConnectionManager(provider or self.provider)
        service = ProviderService(
            connection_manager,
            routing_manager=StubRoutingManager(),
            authorization_manager=self.auth,
            secret_manager=secret_manager,
        )
        service.finalize_started_request = lambda **kwargs: {
            "finalized": True, "status": "FINALIZED",
        }
        return service

    def test_routed_execution_passes_lease_and_closes_it_after_adapter_returns(self):
        service = self._service(secret_manager=self.secrets)
        observed = {}

        def adapter(**kwargs):
            lease = kwargs["provider_credential_lease"]
            self.assertIsInstance(lease, SecretLease)
            self.assertNotIn("provider_credential", kwargs)
            observed["lease"] = lease
            observed["value"] = lease.reveal()
            observed["provider_id"] = kwargs["provider_id"]
            return {
                "started": True, "success": False,
                "actual_cost": 0.0, "charge_outcome": "not_incurred",
            }

        result = service.execute_routed_request(
            category="image", project_id="film", request_id="managed-secret-request",
            estimated_cost=0.0, adapter=adapter, modality="image",
            principal_id="admin-1", use_managed_credential=True,
        )
        self.assertEqual(observed["value"], "integration-secret-value")
        self.assertEqual(observed["provider_id"], "managed-image")
        self.assertTrue(observed["lease"].closed)
        with self.assertRaisesRegex(SecretManagerError, "SECRET_LEASE_CLOSED"):
            observed["lease"].reveal()
        self.assertNotIn("integration-secret-value", repr(result))
        self.assertNotIn("integration-secret-value", repr(service.__dict__))
        self.assertEqual(result["status"], "EXECUTION_FAILED")

    def test_routed_execution_closes_lease_after_ambiguous_adapter_exception(self):
        service = self._service(secret_manager=self.secrets)
        captured = {}

        def adapter(**kwargs):
            captured["lease"] = kwargs["provider_credential_lease"]
            self.assertEqual(captured["lease"].reveal(), "integration-secret-value")
            raise TimeoutError("private endpoint and token detail")

        result = service.execute_routed_request(
            category="image", project_id="film", request_id="managed-secret-timeout",
            estimated_cost=0.0, adapter=adapter, modality="image",
            principal_id="admin-1", use_managed_credential=True,
        )
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertTrue(result["reservations_retained"])
        self.assertTrue(captured["lease"].closed)
        self.assertNotIn("private endpoint", repr(result))
        self.assertNotIn("integration-secret-value", repr(result))

    def test_managed_execution_fails_before_adapter_when_secret_resolution_fails(self):
        class MissingBackend:
            def resolve_secret(self, reference, **kwargs):
                raise SecretManagerError("SECRET_NOT_FOUND")

        missing = ProviderSecretsManager(backends={"vault": MissingBackend()})
        service = self._service(secret_manager=missing)
        calls = []
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="managed-secret-missing",
            estimated_cost=0.0, adapter=lambda **kwargs: calls.append(kwargs),
            modality="image", principal_id="admin-1", use_managed_credential=True,
        )
        self.assertEqual(result["status"], "SECRET_NOT_FOUND")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])

    def test_managed_execution_rejects_caller_supplied_credential(self):
        service = self._service(secret_manager=self.secrets)
        calls = []
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="managed-secret-injected",
            estimated_cost=0.0, adapter=lambda **kwargs: calls.append(kwargs),
            modality="image", principal_id="admin-1", use_managed_credential=True,
            adapter_options={"provider_credential": "attacker-value"},
        )
        self.assertEqual(result["status"], "CALLER_SUPPLIED_MANAGED_CREDENTIAL_REJECTED")
        self.assertEqual(calls, [])
        self.assertNotIn("attacker-value", repr(result))

    def test_rotation_requires_admin_and_explicit_confirmation_at_service_boundary(self):
        service = self._service(secret_manager=self.secrets)
        denied = service.rotate_provider_secret(
            "managed-image", idempotency_key="rotation-0001",
            confirmed=True, principal_id="viewer-1",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertFalse(denied["rotated"])
        self.assertEqual(self.backend.rotations, [])
        not_confirmed = service.rotate_provider_secret(
            "managed-image", idempotency_key="rotation-0001",
            confirmed=False, principal_id="admin-1",
        )
        self.assertEqual(not_confirmed["status"], "ROTATION_CONFIRMATION_REQUIRED")
        self.assertFalse(not_confirmed["rotated"])
        self.assertEqual(self.backend.rotations, [])
        rotated = service.rotate_provider_secret(
            "managed-image", idempotency_key="rotation-0001",
            confirmed=True, principal_id="admin-1",
        )
        self.assertEqual(rotated["status"], "PROVIDER_SECRET_ROTATED")
        self.assertTrue(rotated["rotated"])
        self.assertEqual(rotated["version_id"], "v2")
        self.assertNotIn("rotated-secret", repr(rotated))
        self.assertEqual(len(self.backend.rotations), 1)

    def test_rotation_fails_closed_when_secret_reference_is_missing(self):
        service = self._service(
            provider={"id": "managed-image", "name": "Managed Image"},
            secret_manager=self.secrets,
        )
        result = service.rotate_provider_secret(
            "managed-image", idempotency_key="rotation-0002",
            confirmed=True, principal_id="admin-1",
        )
        self.assertEqual(result["status"], "PROVIDER_SECRET_REFERENCE_REQUIRED")
        self.assertFalse(result["rotated"])
        self.assertEqual(self.backend.rotations, [])


if __name__ == "__main__":
    unittest.main()
