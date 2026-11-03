"""Least-privilege provider authorization regression tests."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_authorization import ProviderAuthorizationManager


class ProviderAuthorizationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "authorization.json"
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manager = ProviderAuthorizationManager(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unknown_principal_and_operation_fail_closed(self):
        self.assertEqual(self.manager.authorize("unknown", "provider.generate")["status"], "PRINCIPAL_NOT_FOUND")
        self.manager.assign_role("viewer-1", "viewer")
        self.assertEqual(self.manager.authorize("viewer-1", "provider.root")["status"], "UNKNOWN_OPERATION")

    def test_roles_receive_only_explicit_permissions(self):
        self.manager.assign_role("viewer-1", "viewer")
        self.manager.assign_role("creator-1", "creator")
        self.assertTrue(self.manager.authorize("viewer-1", "provider.read")["allowed"])
        self.assertFalse(self.manager.authorize("viewer-1", "provider.generate")["allowed"])
        self.assertTrue(self.manager.authorize("creator-1", "provider.generate")["allowed"])
        self.assertFalse(self.manager.authorize("creator-1", "billing.approve")["allowed"])

    def test_policy_simulation_is_an_explicit_admin_only_operation(self):
        self.manager.assign_role("admin-1", "admin")
        self.manager.assign_role("operator-1", "operator")
        self.manager.assign_role("viewer-1", "viewer")
        self.assertTrue(
            self.manager.authorize("admin-1", "provider.policy.simulate")["allowed"]
        )
        self.assertEqual(
            self.manager.authorize("operator-1", "provider.policy.simulate")["status"],
            "ACCESS_DENIED",
        )
        self.assertEqual(
            self.manager.authorize("viewer-1", "provider.policy.simulate")["status"],
            "ACCESS_DENIED",
        )

    def test_disabled_principal_cannot_authorize_operations(self):
        self.manager.assign_role("operator-1", "operator")
        self.assertTrue(self.manager.authorize("operator-1", "provider.verify")["allowed"])
        self.assertEqual(self.manager.disable_principal("operator-1")["status"], "PRINCIPAL_DISABLED")
        self.assertEqual(self.manager.authorize("operator-1", "provider.verify")["status"], "PRINCIPAL_DISABLED")

    def test_role_assignments_persist_and_reject_malformed_records(self):
        self.manager.assign_role("billing-1", "billing_manager")
        reloaded = ProviderAuthorizationManager(self.path, clock=lambda: self.now)
        self.assertTrue(reloaded.authorize("billing-1", "billing.approve")["allowed"])
        self.assertFalse(reloaded.authorize("billing-1", "provider.generate")["allowed"])
        self.path.write_text(json.dumps({"principals": {"bad id": {"role": "admin", "enabled": True, "updated_at": self.now.isoformat()}}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderAuthorizationManager(self.path, clock=lambda: self.now)

    def test_authorization_store_enforces_version_timestamp_and_principal_capacity(self):
        limited = ProviderAuthorizationManager(
            self.path, clock=lambda: self.now, max_principals=1
        )
        self.assertTrue(limited.assign_role("creator-1", "creator")["saved"])
        full = limited.assign_role("viewer-1", "viewer")
        self.assertEqual(full["status"], "PRINCIPAL_CAPACITY_REACHED")
        self.assertTrue(limited.assign_role("creator-1", "operator")["saved"])

        self.path.write_text(json.dumps({
            "version": "2.0",
            "principals": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "authorization state schema"):
            ProviderAuthorizationManager(self.path, clock=lambda: self.now)

        self.path.write_text(json.dumps({
            "version": "1.0",
            "principals": {
                "creator-1": {
                    "role": "creator", "enabled": True, "updated_at": "not-a-time"
                }
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "authorization timestamp"):
            ProviderAuthorizationManager(self.path, clock=lambda: self.now)


    def test_access_management_is_an_explicit_admin_only_permission(self):
        self.manager.assign_role("admin-1", "admin")
        self.manager.assign_role("operator-1", "operator")
        self.manager.assign_role("viewer-1", "viewer")
        self.assertTrue(
            self.manager.authorize("admin-1", "provider.access.manage")["allowed"]
        )
        for principal in ("operator-1", "viewer-1"):
            result = self.manager.authorize(principal, "provider.access.manage")
            self.assertFalse(result["allowed"])
            self.assertEqual(result["status"], "ACCESS_DENIED")

    def test_last_active_admin_cannot_be_disabled_or_demoted(self):
        self.manager.assign_role("admin-1", "admin")
        self.assertEqual(
            self.manager.disable_principal("admin-1")["status"],
            "LAST_ACTIVE_ADMIN_REQUIRED",
        )
        self.assertEqual(
            self.manager.assign_role("admin-1", "operator")["status"],
            "LAST_ACTIVE_ADMIN_REQUIRED",
        )
        self.manager.assign_role("admin-2", "admin")
        self.assertTrue(self.manager.disable_principal("admin-1")["saved"])
        self.assertTrue(self.manager.authorize("admin-2", "provider.access.manage")["allowed"])


    def test_provider_scopes_are_operation_specific_and_fail_closed(self):
        self.manager.assign_role("creator-1", "creator")
        saved = self.manager.set_provider_scopes(
            "creator-1", {"provider.generate": ["image-main", "image-backup"]}
        )
        self.assertTrue(saved["saved"])
        self.assertTrue(self.manager.authorize(
            "creator-1", "provider.generate", provider_id="image-main"
        )["allowed"])
        self.assertEqual(self.manager.authorize(
            "creator-1", "provider.generate", provider_id="voice"
        )["status"], "PROVIDER_SCOPE_DENIED")
        self.assertEqual(self.manager.authorize(
            "creator-1", "provider.generate"
        )["status"], "PROVIDER_SCOPE_REQUIRED")
        self.assertEqual(self.manager.authorize(
            "creator-1", "provider.read", provider_id="image-main"
        )["status"], "PROVIDER_SCOPE_DENIED")
        candidates = self.manager.authorize(
            "creator-1", "provider.generate", provider_id="*"
        )
        self.assertTrue(candidates["allowed"])
        self.assertEqual(candidates["allowed_provider_ids"], ["image-main", "image-backup"])

    def test_provider_scopes_validate_operation_ids_and_role_permissions(self):
        self.manager.assign_role("creator-1", "creator")
        self.assertEqual(
            self.manager.set_provider_scopes(
                "creator-1", {"provider.configure": ["image"]}
            )["status"],
            "ROLE_OPERATION_NOT_PERMITTED",
        )
        for bad in (
            {"provider.generate": ["bad id"]},
            {"provider.generate": ["image", "image"]},
            {"provider.root": ["image"]},
            {"provider.generate": "image"},
        ):
            with self.subTest(scopes=bad):
                result = self.manager.set_provider_scopes("creator-1", bad)
                self.assertFalse(result["saved"])
        self.assertTrue(self.manager.set_provider_scopes(
            "creator-1", {"provider.generate": ["image"]}
        )["saved"])
        with self.assertRaises(ValueError):
            self.path.write_text(json.dumps({
                "version":"1.0","principals":{
                    "creator-1":{
                        "role":"creator","enabled":True,
                        "updated_at":self.now.isoformat(),
                        "provider_scopes":{"provider.configure":["image"]},
                    }
                }
            }),encoding="utf-8")
            ProviderAuthorizationManager(self.path, clock=lambda:self.now)

    def test_provider_scope_restrictions_survive_role_changes(self):
        self.manager.assign_role("creator-1", "creator")
        self.manager.set_provider_scopes("creator-1", {
            "provider.generate": ["image"]
        })
        self.manager.assign_role("creator-1", "operator")
        self.assertEqual(self.manager.authorize(
            "creator-1", "provider.generate", provider_id="voice"
        )["status"], "PROVIDER_SCOPE_DENIED")
        self.assertTrue(self.manager.authorize(
            "creator-1", "provider.generate", provider_id="image"
        )["allowed"])

    def test_invalid_assignments_fail_closed(self):
        self.assertEqual(self.manager.assign_role("bad id", "admin")["status"], "INVALID_PRINCIPAL_ID")
        self.assertEqual(self.manager.assign_role("creator-1", "root")["status"], "INVALID_ROLE")
        self.assertEqual(self.manager.assign_role("creator-1", "creator", enabled="yes")["status"], "INVALID_ENABLED_FLAG")


if __name__ == "__main__":
    unittest.main()
