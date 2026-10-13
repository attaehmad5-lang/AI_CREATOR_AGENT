import unittest

from projects.provider_store.access_control import ProviderAccessControl
from projects.provider_store.audit_log import ProviderAuditLog


class ProviderAccessControlTests(unittest.TestCase):
    def setUp(self):
        self.acl = ProviderAccessControl()

    def test_missing_principal_fails_closed(self):
        result = self.acl.authorize(
            None, action="provider.route", project_id="film"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "AUTHENTICATED_PRINCIPAL_REQUIRED")

    def test_creator_can_route_but_cannot_manage_providers(self):
        principal = {"subject": "user-1", "roles": ["creator"], "projects": ["film"]}
        allowed = self.acl.authorize(principal, action="provider.route", project_id="film")
        denied = self.acl.authorize(principal, action="provider.manage", project_id="film")
        self.assertTrue(allowed["allowed"])
        self.assertEqual(denied["status"], "PERMISSION_DENIED")

    def test_paid_execution_requires_explicit_role_permission(self):
        creator = {"subject": "user-1", "roles": ["creator"]}
        operator = {"subject": "user-2", "roles": ["operator"], "projects": ["film"]}
        self.assertEqual(
            self.acl.authorize(creator, action="provider.paid.execute", project_id="film")["status"],
            "PERMISSION_DENIED",
        )
        self.assertTrue(
            self.acl.authorize(operator, action="provider.paid.execute", project_id="film")["allowed"]
        )

    def test_project_provider_and_capability_scopes_are_enforced(self):
        principal = {
            "subject": "user-1", "roles": ["creator"],
            "projects": ["film"], "providers": ["image-a"], "capabilities": ["text_to_image"],
        }
        self.assertTrue(self.acl.authorize(
            principal, action="provider.route", project_id="film",
            provider_id="image-a", capability="text_to_image",
        )["allowed"])
        self.assertEqual(self.acl.authorize(
            principal, action="provider.route", project_id="other"
        )["status"], "PROJECT_SCOPE_DENIED")
        self.assertEqual(self.acl.authorize(
            principal, action="provider.route", project_id="film",
            provider_id="image-b",
        )["status"], "PROVIDER_SCOPE_DENIED")
        self.assertEqual(self.acl.authorize(
            principal, action="provider.route", project_id="film",
            capability="text_to_video",
        )["status"], "CAPABILITY_SCOPE_DENIED")

    def test_provider_scope_is_explicit_and_validated(self):
        unrestricted = self.acl.provider_scope({"subject": "user-1", "roles": ["creator"]})
        self.assertTrue(unrestricted["valid"])
        self.assertFalse(unrestricted["restricted"])
        restricted = self.acl.provider_scope({
            "subject": "user-1", "roles": ["creator"], "providers": ["image-a"]
        })
        self.assertTrue(restricted["valid"])
        self.assertTrue(restricted["restricted"])
        self.assertEqual(restricted["providers"], ["image-a"])
        invalid = self.acl.provider_scope({
            "subject": "user-1", "roles": ["creator"], "providers": "image-a"
        })
        self.assertFalse(invalid["valid"])

    def test_malformed_scope_fails_closed(self):
        principal = {"subject": "user-1", "roles": ["creator"], "projects": "film"}
        result = self.acl.authorize(principal, action="provider.route", project_id="film")
        self.assertEqual(result["status"], "INVALID_PROJECT_SCOPE")

    def test_project_scope_is_required_even_for_known_role(self):
        result = self.acl.authorize(
            {"subject": "user-1", "roles": ["creator"]},
            action="provider.route", project_id="film",
        )
        self.assertEqual(result["status"], "PROJECT_SCOPE_REQUIRED")

    def test_unknown_role_fails_closed(self):
        result = self.acl.authorize(
            {"subject": "user-1", "roles": ["superuser"]},
            action="provider.route", project_id="film",
        )
        self.assertEqual(result["status"], "UNKNOWN_PRINCIPAL_ROLE")

    def test_custom_role_permissions_are_validated(self):
        custom = ProviderAccessControl({"auditor": frozenset({"provider.audit.read"})})
        self.assertTrue(custom.authorize(
            {"subject": "auditor-1", "roles": ["auditor"], "projects": ["film"]},
            action="provider.audit.read", project_id="film",
        )["allowed"])
        with self.assertRaises(ValueError):
            ProviderAccessControl({"bad role": {"provider.route"}})
        with self.assertRaises(ValueError):
            ProviderAccessControl({"creator": {"*"}})

    def test_authorization_decisions_are_audited(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.jsonl"
            acl = ProviderAccessControl(audit_logger=ProviderAuditLog(path))
            allowed = acl.authorize(
                {"subject": "user-1", "roles": ["creator"], "projects": ["film"]},
                action="provider.route", project_id="film",
            )
            denied = acl.authorize(
                {"subject": "user-1", "roles": ["creator"], "projects": ["film"]},
                action="provider.paid.execute", project_id="film",
            )
            self.assertTrue(allowed["allowed"])
            self.assertFalse(denied["allowed"])
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["decision"] for event in events], ["allowed", "denied"])

    def test_allowed_operation_fails_closed_if_audit_write_fails(self):
        class BrokenAudit:
            def record(self, event):
                return {"status": "AUDIT_WRITE_FAILED", "saved": False}
        acl = ProviderAccessControl(audit_logger=BrokenAudit())
        result = acl.authorize(
            {"subject": "owner-1", "roles": ["owner"], "projects": ["film"]},
            action="provider.route", project_id="film",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "AUDIT_LOG_WRITE_FAILED")

    def test_denial_response_does_not_disclose_principal_or_secrets(self):
        result = self.acl.authorize(
            {"subject": "user-1", "roles": ["creator"], "token": "sensitive-value"},
            action="provider.credential.manage", project_id="film",
        )
        self.assertEqual(result["status"], "PERMISSION_DENIED")
        self.assertNotIn("sensitive-value", repr(result))


if __name__ == "__main__":
    unittest.main()
