"""Regression tests for workspace policy authorization."""
import math
import unittest
from projects.workspace_permissions import (
    PolicyError, PolicyRule, WorkspacePolicy, normalize_scope,
)


class WorkspacePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = WorkspacePolicy()

    def test_default_is_deny(self):
        self.assertFalse(self.policy.authorize("alice", "file.read", "docs/a.txt"))
        self.assertFalse(self.policy.authorize("alice", "file.read"))

    def test_root_scope_rule_applies_to_workspace(self):
        self.policy.add_rule(PolicyRule("alice", "file.read", "."))
        self.assertTrue(self.policy.authorize("alice", "file.read", "docs/a.txt"))

    def test_role_grants_only_expected_capabilities(self):
        self.policy.assign_role("alice", "viewer")
        self.assertTrue(self.policy.authorize("alice", "file.read", "docs/a.txt"))
        self.assertFalse(self.policy.authorize("alice", "file.delete", "docs/a.txt"))

    def test_explicit_deny_overrides_role(self):
        self.policy.assign_role("alice", "owner")
        self.policy.add_rule(PolicyRule("alice", "file.read", "private", "deny"))
        self.assertFalse(self.policy.authorize("alice", "file.read", "private/a.txt"))
        self.assertTrue(self.policy.authorize("alice", "file.read", "public/a.txt"))

    def test_wildcard_subject_and_scope(self):
        self.policy.add_rule(PolicyRule("*", "file.read", "shared/*", "allow"))
        self.assertTrue(self.policy.authorize("bob", "file.read", "shared/readme.txt"))
        self.assertFalse(self.policy.authorize("bob", "file.read", "private/readme.txt"))

    def test_expired_rule_does_not_grant_access(self):
        self.policy.add_rule(PolicyRule("alice", "file.read", "docs", "allow", expires_at=1))
        self.assertFalse(self.policy.authorize("alice", "file.read", "docs/a.txt"))

    def test_rejects_path_traversal_and_absolute_scope(self):
        for scope in ("../secret", "docs/../secret", "/etc/passwd", r"..\\secret"):
            with self.subTest(scope=scope), self.assertRaises(PolicyError):
                normalize_scope(scope)

    def test_scope_rejects_windows_drive_paths_control_chars_and_oversize(self):
        for scope in ("C:/secret", r"D:\\private", "docs/\x00secret", "docs/\nsecret", "x" * 1025):
            with self.subTest(scope=repr(scope)), self.assertRaises(PolicyError):
                normalize_scope(scope)

    def test_scope_normalization_preserves_safe_nested_paths(self):
        self.assertEqual(normalize_scope(r"docs\\drafts\\file.txt"), "docs/drafts/file.txt")
        self.assertEqual(normalize_scope("./docs/./file.txt"), "docs/file.txt")

    def test_policy_rule_rejects_nonfinite_or_boolean_expiry(self):
        for expiry in (float("nan"), float("inf"), float("-inf"), True, False, 0, -1):
            with self.subTest(expiry=expiry), self.assertRaises(PolicyError):
                PolicyRule("alice", "file.read", expires_at=expiry)

    def test_policy_rule_rejects_malformed_permission_and_effect_types(self):
        for kwargs in (
            {"permission": []},
            {"effect": []},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyError):
                PolicyRule("alice", "file.read", **kwargs) if "permission" not in kwargs else PolicyRule("alice", kwargs["permission"])

    def test_unknown_permission_and_role_are_rejected(self):
        with self.assertRaises(PolicyError):
            self.policy.authorize("alice", "root.access", ".")
        with self.assertRaises(PolicyError):
            self.policy.assign_role("alice", "superuser")

    def test_revoke_role_and_audit_are_available_and_defensive(self):
        self.policy.assign_role("alice", "editor")
        self.assertTrue(self.policy.revoke_role("alice"))
        self.assertFalse(self.policy.authorize("alice", "file.read", "a.txt"))
        events = self.policy.audit_events(limit=2)
        self.assertEqual(len(events), 2)
        events[0]["subject"] = "tampered"
        self.assertNotEqual(self.policy.audit_events(limit=2)[0]["subject"], "tampered")

    def test_remove_rule_reports_missing_rule(self):
        rule = PolicyRule("alice", "file.read", "docs")
        self.assertFalse(self.policy.remove_rule(rule))
        self.policy.add_rule(rule)
        self.assertTrue(self.policy.remove_rule(rule))


if __name__ == "__main__":
    unittest.main()
