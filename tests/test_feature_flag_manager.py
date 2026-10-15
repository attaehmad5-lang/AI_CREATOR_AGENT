import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.feature_flag_manager import ProviderFeatureFlagManager


class ProviderFeatureFlagManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "flags.json"
        self.now = [datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)]
        self.manager = ProviderFeatureFlagManager(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_flags_fail_closed_and_full_rollout_is_enabled(self):
        missing = self.manager.evaluate("new_router", project_id="film")
        self.assertFalse(missing["enabled"])
        self.assertEqual(missing["status"], "FLAG_NOT_FOUND")
        saved = self.manager.set_flag("new_router", enabled=True, rollout_percent=100)
        self.assertTrue(saved["saved"])
        result = self.manager.evaluate("new_router", project_id="film")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["status"], "FLAG_ENABLED")

    def test_project_allow_deny_scopes_and_kill_switch(self):
        self.manager.set_flag(
            "new_router", enabled=True, rollout_percent=100,
            allowed_projects=["film", "shorts"], denied_projects=["shorts"],
        )
        self.assertTrue(self.manager.evaluate("new_router", project_id="film")["enabled"])
        self.assertEqual(self.manager.evaluate("new_router", project_id="other")["status"], "PROJECT_NOT_ALLOWLISTED")
        self.assertEqual(self.manager.evaluate("new_router", project_id="shorts")["status"], "PROJECT_DENIED")
        self.manager.set_flag("new_router", enabled=False)
        self.assertEqual(self.manager.evaluate("new_router", project_id="film")["status"], "FLAG_DISABLED")

    def test_rollout_assignment_is_deterministic_and_bounded(self):
        self.manager.set_flag("new_router", enabled=True, rollout_percent=50)
        first = self.manager.evaluate("new_router", project_id="film", subject_id="user-17")
        second = self.manager.evaluate("new_router", project_id="film", subject_id="user-17")
        self.assertEqual(first, second)
        self.assertIn(first["status"], {"FLAG_ENABLED", "OUTSIDE_ROLLOUT"})
        self.assertGreaterEqual(first["bucket"], 0)
        self.assertLess(first["bucket"], 10000)

    def test_expired_flags_fail_closed(self):
        self.manager.set_flag(
            "temporary", enabled=True, rollout_percent=100,
            expires_at="2026-10-15T12:00:00+00:00",
        )
        result = self.manager.evaluate("temporary", project_id="film")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["status"], "FLAG_EXPIRED")

    def test_invalid_inputs_and_unsafe_scope_overlap_are_rejected(self):
        self.assertEqual(self.manager.set_flag("../unsafe", enabled=True)["status"], "INVALID_FLAG_NAME")
        self.assertEqual(self.manager.set_flag("x", enabled="true")["status"], "INVALID_FLAG_STATE")
        self.assertEqual(self.manager.set_flag("x", enabled=True, rollout_percent=float("nan"))["status"], "INVALID_ROLLOUT_PERCENT")
        self.assertEqual(
            self.manager.set_flag("x", enabled=True, allowed_projects=["film"], denied_projects=["film"])["status"],
            "INVALID_FLAG_CONFIGURATION",
        )
        self.assertEqual(self.manager.evaluate("x", project_id="../bad")["status"], "INVALID_PROJECT_ID")
        self.assertEqual(
            self.manager.evaluate("x", project_id="film", subject_id="bad subject")["status"],
            "INVALID_SUBJECT_ID",
        )

    def test_malformed_persisted_store_fails_closed(self):
        self.path.write_text('{"version":"99.0","flags":{}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderFeatureFlagManager(self.path, clock=lambda: self.now[0])
        self.path.write_text("{invalid-json", encoding="utf-8")
        result = self.manager.evaluate("flag", project_id="film")
        self.assertFalse(result["enabled"])
        self.assertEqual(result["status"], "FLAG_STORE_INVALID")

    def test_updates_persist_across_manager_instances(self):
        self.manager.set_flag("safe_rollout", enabled=True, rollout_percent=100)
        loaded = ProviderFeatureFlagManager(self.path, clock=lambda: self.now[0])
        self.assertTrue(loaded.evaluate("safe_rollout", project_id="film")["enabled"])


if __name__ == "__main__":
    unittest.main()
