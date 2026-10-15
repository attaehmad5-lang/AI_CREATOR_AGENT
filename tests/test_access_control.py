import unittest

from projects.provider_store.access_control import ProviderAccessController


class ProviderAccessControllerTests(unittest.TestCase):
    def setUp(self):
        self.policy = ProviderAccessController()
        self.principal = {
            "subject_id": "user-17",
            "tenant_id": "tenant-a",
            "roles": ["operator"],
            "project_ids": ["film-a"],
            # Untrusted incidental fields must never be echoed into the audit event.
            "access_token": "never-log-this-token",
        }
        self.resource = {
            "provider_id": "huggingface",
            "tenant_id": "tenant-a",
            "project_id": "film-a",
            "api_key": "never-log-this-key",
        }

    def test_operator_can_route_only_inside_authorized_scope(self):
        result = self.policy.authorize(self.principal, "provider:route", self.resource)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["reason"], "PERMITTED")

    def test_reader_cannot_connect_or_configure_provider(self):
        principal = {**self.principal, "roles": ["reader"]}
        self.assertFalse(self.policy.authorize(principal, "provider:connect", self.resource)["allowed"])
        self.assertFalse(self.policy.authorize(principal, "provider:configure", self.resource)["allowed"])

    def test_tenant_mismatch_denies_even_admin(self):
        principal = {**self.principal, "roles": ["admin"]}
        resource = {**self.resource, "tenant_id": "tenant-b"}
        result = self.policy.authorize(principal, "provider:disable", resource)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "TENANT_SCOPE_MISMATCH")

    def test_project_mismatch_denies_even_admin(self):
        principal = {**self.principal, "roles": ["admin"]}
        resource = {**self.resource, "project_id": "film-b"}
        result = self.policy.authorize(principal, "provider:configure", resource)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "PROJECT_SCOPE_MISMATCH")

    def test_unknown_or_mixed_roles_fail_closed(self):
        for roles in (["unknown"], ["operator", "unknown"], [], "admin", None):
            with self.subTest(roles=roles):
                result = self.policy.authorize(
                    {**self.principal, "roles": roles}, "provider:route", self.resource
                )
                self.assertFalse(result["allowed"])

    def test_unknown_actions_and_missing_scope_fail_closed(self):
        self.assertFalse(self.policy.authorize(self.principal, "provider:root", self.resource)["allowed"])
        self.assertFalse(self.policy.authorize(self.principal, ["provider:route"], self.resource)["allowed"])
        self.assertFalse(self.policy.authorize(
            {**self.principal, "tenant_id": ""}, "provider:route", self.resource
        )["allowed"])
        self.assertFalse(self.policy.authorize(
            self.principal, "provider:route", {**self.resource, "tenant_id": ""}
        )["allowed"])
        missing_project = self.policy.authorize(
            self.principal, "provider:route", {key: value for key, value in self.resource.items() if key != "project_id"}
        )
        self.assertFalse(missing_project["allowed"])
        self.assertEqual(missing_project["reason"], "PROJECT_SCOPE_REQUIRED")

    def test_secret_read_and_purchase_are_never_granted(self):
        principal = {**self.principal, "roles": ["admin", "security_admin"]}
        for action in ("credential:read", "secret:read", "token:read", "provider:purchase"):
            with self.subTest(action=action):
                result = self.policy.authorize(principal, action, self.resource)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], "SENSITIVE_ACTION_FORBIDDEN")

    def test_custom_policy_cannot_grant_forbidden_actions(self):
        with self.assertRaises(ValueError):
            ProviderAccessController(role_permissions={"superuser": {"credential:read"}})
        with self.assertRaises(ValueError):
            ProviderAccessController(role_permissions={"role": {"arbitrary:execute"}})

    def test_audit_event_contains_only_safe_allowlisted_fields(self):
        result = self.policy.authorize(self.principal, "provider:route", self.resource)
        rendered = repr(result["audit_event"])
        self.assertNotIn("never-log-this-token", rendered)
        self.assertNotIn("never-log-this-key", rendered)
        self.assertEqual(set(result["audit_event"]), {
            "subject_id", "tenant_id", "action", "provider_id",
            "resource_tenant_id", "project_id", "allowed", "reason",
        })

    def test_security_admin_can_rotate_but_not_read_credentials(self):
        principal = {**self.principal, "roles": ["security_admin"]}
        self.assertTrue(self.policy.authorize(principal, "credential:rotate", self.resource)["allowed"])
        self.assertFalse(self.policy.authorize(principal, "credential:read", self.resource)["allowed"])

    def test_permissions_are_immutable_sorted_snapshots(self):
        self.assertEqual(
            self.policy.permissions_for("reader"),
            ("health:read", "provider:read", "usage:read"),
        )
        self.assertEqual(self.policy.permissions_for("missing"), ())


if __name__ == "__main__":
    unittest.main()
