import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_admin_manager import ProviderAdminManager


class ProviderAdminManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "admin-audit.json"
        self.calls = []
        self.manager = ProviderAdminManager(
            self.path,
            authorizer=lambda **kwargs: True,
            handlers={
                "provider.enable": lambda **kwargs: self.calls.append(kwargs) or {"success": True},
                "policy.activate": lambda **kwargs: {"success": True},
                "catalog.restore": lambda **kwargs: {"success": False},
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_authorizer_and_handler_fail_closed(self):
        no_auth = ProviderAdminManager(self.path.with_name("no-auth.json"))
        result = no_auth.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="req-1",
        )
        self.assertEqual(result["status"], "ADMIN_AUTHORIZER_REQUIRED")
        unavailable = ProviderAdminManager(
            self.path.with_name("no-handler.json"),
            authorizer=lambda **kwargs: True,
        )
        result = unavailable.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="req-2",
        )
        self.assertEqual(result["status"], "ADMIN_HANDLER_UNAVAILABLE")

    def test_denied_or_failed_authorization_never_calls_handler(self):
        denied = ProviderAdminManager(
            self.path.with_name("denied.json"),
            authorizer=lambda **kwargs: False,
            handlers={"provider.enable": lambda **kwargs: self.fail("must not run")},
        )
        self.assertEqual(denied.execute(
            subject_id="user-1", operation="provider.enable",
            resource="provider-a", request_id="denied-1",
        )["status"], "ADMIN_FORBIDDEN")
        failed = ProviderAdminManager(
            self.path.with_name("auth-error.json"),
            authorizer=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")),
            handlers={"provider.enable": lambda **kwargs: self.fail("must not run")},
        )
        result = failed.execute(
            subject_id="user-1", operation="provider.enable",
            resource="provider-a", request_id="failed-auth",
        )
        self.assertEqual(result["status"], "ADMIN_AUTHORIZATION_FAILED")
        self.assertNotIn("secret detail", repr(result))
        self.assertEqual(self.calls, [])

    def test_success_is_audited_without_persisting_parameters(self):
        result = self.manager.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="enable-1",
            parameters={"reason": "approved change", "ticket": "OPS-123"},
        )
        self.assertEqual(result["status"], "SUCCEEDED")
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["requests"]["enable-1"]["status"], "SUCCEEDED")
        self.assertEqual(stored["events"][0]["status"], "SUCCEEDED")
        self.assertNotIn("approved change", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("OPS-123", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.calls[0]["parameters"]["reason"], "approved change")

    def test_secret_like_parameter_names_are_rejected(self):
        for parameters in (
            {"api_key": "not-persisted"},
            {"nested": {"access_token": "not-persisted"}},
            {"Authorization": "Bearer not-persisted"},
        ):
            with self.subTest(parameters=list(parameters)):
                result = self.manager.execute(
                    subject_id="admin-1", operation="provider.enable",
                    resource="provider-a", request_id="secret-" + str(len(self.calls)),
                    parameters=parameters,
                )
                self.assertEqual(result["status"], "SECRET_FIELD_NOT_ALLOWED")
        self.assertEqual(self.calls, [])

    def test_request_id_replay_is_idempotent_and_conflicts_fail_closed(self):
        first = self.manager.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="same-id",
        )
        replay = self.manager.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="same-id",
        )
        conflict = self.manager.execute(
            subject_id="admin-1", operation="provider.disable",
            resource="provider-a", request_id="same-id",
        )
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(replay["status"], "ADMIN_REQUEST_REPLAY")
        self.assertEqual(conflict["status"], "ADMIN_REQUEST_ID_CONFLICT")
        self.assertEqual(len(self.calls), 1)

    def test_failed_handler_has_safe_terminal_status(self):
        result = self.manager.execute(
            subject_id="admin-1", operation="catalog.restore",
            resource="catalog", request_id="restore-1",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["allowed"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["requests"]["restore-1"]["status"], "FAILED")

    def test_invalid_operations_and_identifiers_are_rejected(self):
        self.assertEqual(self.manager.execute(
            subject_id="admin-1", operation="delete_everything",
            resource="provider-a", request_id="bad-op",
        )["status"], "UNSUPPORTED_ADMIN_OPERATION")
        self.assertEqual(self.manager.execute(
            subject_id=" ", operation="provider.enable",
            resource="provider-a", request_id="bad-subject",
        )["status"], "INVALID_ADMIN_REQUEST")
        self.assertEqual(self.manager.execute(
            subject_id="admin-1", operation="provider.enable",
            resource="provider-a", request_id="bad-params", parameters=[],
        )["status"], "INVALID_ADMIN_PARAMETERS")

    def test_corrupt_persisted_audit_state_fails_closed(self):
        self.path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderAdminManager(self.path, authorizer=lambda **kwargs: True)

    def test_operation_list_is_explicit_and_stable(self):
        operations = self.manager.list_operations()
        self.assertEqual(operations, sorted(ProviderAdminManager.OPERATIONS))
        self.assertNotIn("*", operations)


if __name__ == "__main__":
    unittest.main()
