"""OIDC principal interoperability at privacy deletion/retention boundaries."""
import unittest
from datetime import datetime, timezone

from projects.provider_store.privacy_data_lifecycle import PrivacyDataLifecycleManager
from projects.provider_store.provider_service import ProviderService


class AuthenticatedPrincipal(str):
    pass


class IdentityAuthenticator:
    def __init__(self):
        self.principal = AuthenticatedPrincipal("admin-oidc-01")

    def authenticate(self, token):
        if token != "signed.header.signature":
            raise ValueError("invalid token")
        return self.principal

    def is_authenticated_principal(self, value):
        return value is self.principal


class AuthorizationManager:
    def __init__(self):
        self.calls = []

    def authorize(self, principal_id, permission):
        self.calls.append((principal_id, permission))
        return {"allowed": principal_id == "admin-oidc-01" and permission == "provider.access.manage"}


class TenantIsolationManager:
    def __init__(self):
        self.calls = []

    def authorize_project(self, tenant_id, project_id, principal_id):
        self.calls.append((tenant_id, project_id, principal_id))
        return {"allowed": principal_id == "admin-oidc-01"}


class Store:
    def __init__(self):
        self.calls = []

    def delete_subject_data(self, **kwargs):
        self.calls.append(("delete", dict(kwargs)))
        return {"confirmed": True, "status": "DELETED", "deleted_count": 1}

    def purge_expired_data(self, **kwargs):
        self.calls.append(("purge", dict(kwargs)))
        return {"confirmed": True, "status": "PURGED", "deleted_count": 2}


class ConnectionManager:
    def verify_connectivity(self, *args, **kwargs):
        return {"verified": True}


class PrivacyOIDCPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.identity = IdentityAuthenticator()
        self.authorization = AuthorizationManager()
        self.tenant = TenantIsolationManager()
        self.store = Store()
        self.lifecycle = PrivacyDataLifecycleManager({"primary-db": self.store})
        self.service = ProviderService(
            ConnectionManager(),
            authorization_manager=self.authorization,
            identity_authenticator=self.identity,
            tenant_isolation_manager=self.tenant,
            privacy_lifecycle_manager=self.lifecycle,
        )
        auth = self.service.authenticate_bearer_token("Bearer signed.header.signature")
        self.assertTrue(auth["authenticated"])
        self.principal = auth["principal_id"]

    def test_verified_opaque_oidc_principal_can_delete_subject_data(self):
        result = self.service.delete_subject_data(
            subject_ref="subject-ref-oidc",
            project_id="project-oidc",
            tenant_id="tenant-oidc",
            request_id="delete-request-oidc-01",
            principal_id=self.principal,
            confirmed=True,
        )
        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "SUBJECT_DATA_DELETED")
        self.assertEqual(self.authorization.calls[-1], ("admin-oidc-01", "provider.access.manage"))
        self.assertEqual(self.tenant.calls[-1], ("tenant-oidc", "project-oidc", "admin-oidc-01"))
        self.assertEqual(self.store.calls[0][0], "delete")

    def test_verified_opaque_oidc_principal_can_run_retention_purge(self):
        result = self.service.purge_expired_privacy_data(
            cutoff=datetime(2026, 10, 1, tzinfo=timezone.utc),
            project_id="project-oidc",
            tenant_id="tenant-oidc",
            request_id="purge-request-oidc-01",
            principal_id=self.principal,
            confirmed=True,
        )
        self.assertTrue(result["completed"])
        self.assertEqual(result["status"], "RETENTION_PURGE_CONFIRMED")
        self.assertEqual(self.tenant.calls[-1], ("tenant-oidc", "project-oidc", "admin-oidc-01"))
        self.assertEqual(self.store.calls[0][0], "purge")

    def test_caller_supplied_principal_id_string_is_not_an_oidc_capability(self):
        result = self.service.delete_subject_data(
            subject_ref="subject-ref-oidc",
            project_id="project-oidc",
            tenant_id="tenant-oidc",
            request_id="delete-request-oidc-02",
            principal_id="admin-oidc-01",
            confirmed=True,
        )
        self.assertEqual(result["status"], "AUTHENTICATED_PRINCIPAL_REQUIRED")
        self.assertEqual(self.authorization.calls, [])
        self.assertEqual(self.tenant.calls, [])
        self.assertEqual(self.store.calls, [])


if __name__ == "__main__":
    unittest.main()
