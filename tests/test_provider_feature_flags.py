"""Provider feature flag rollout, scope, and persistence tests."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_feature_flags import ProviderFeatureFlagManager


class ProviderFeatureFlagManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "flags.json"
        self.manager = ProviderFeatureFlagManager(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unknown_and_disabled_flags_fail_closed(self):
        self.assertEqual(self.manager.evaluate(
            "provider_generation", project_id="film", request_id="job-1"
        )["status"], "FEATURE_FLAG_UNKNOWN")
        self.manager.set_flag("provider_generation", enabled=False, rollout_percent=100)
        self.assertFalse(self.manager.evaluate(
            "provider_generation", project_id="film", request_id="job-1"
        )["enabled"])

    def test_project_allow_deny_and_kill_switch_precedence(self):
        self.manager.set_flag(
            "provider_generation", enabled=True, rollout_percent=0,
            allow_projects=["approved-film"], deny_projects=["blocked-film"],
        )
        self.assertTrue(self.manager.evaluate(
            "provider_generation", project_id="approved-film", request_id="job-1"
        )["enabled"])
        self.assertFalse(self.manager.evaluate(
            "provider_generation", project_id="blocked-film", request_id="job-1"
        )["enabled"])
        self.manager.set_flag("provider_generation", enabled=False, rollout_percent=100,
                              allow_projects=["approved-film"])
        self.assertFalse(self.manager.evaluate(
            "provider_generation", project_id="approved-film", request_id="job-1"
        )["enabled"])

    def test_rollout_decision_is_deterministic(self):
        self.manager.set_flag("new_router", enabled=True, rollout_percent=25)
        a = self.manager.evaluate("new_router", project_id="film", request_id="job-123")
        b = self.manager.evaluate("new_router", project_id="film", request_id="job-123")
        self.assertEqual(a, b)
        self.assertIn(a["status"], {"ROLLOUT_ENABLED", "OUTSIDE_ROLLOUT"})

    def test_feature_flag_capacity_preserves_updates_and_limits_scope_size(self):
        limited = ProviderFeatureFlagManager(
            self.path, max_flags=1, max_projects_per_flag=1
        )
        first = limited.set_flag(
            "job_queue", enabled=True, allow_projects=["film-a"]
        )
        too_many_scopes = limited.set_flag(
            "job_queue", enabled=True, allow_projects=["film-a", "film-b"]
        )
        updated = limited.set_flag("job_queue", enabled=False)
        second = limited.set_flag("webhooks", enabled=True)
        self.assertEqual(first["status"], "FEATURE_FLAG_SAVED")
        self.assertEqual(too_many_scopes["status"], "PROJECT_SCOPE_CAPACITY_REACHED")
        self.assertEqual(updated["status"], "FEATURE_FLAG_SAVED")
        self.assertEqual(second["status"], "FEATURE_FLAG_CAPACITY_REACHED")

    def test_feature_flag_store_rejects_unsupported_version_and_duplicate_scopes(self):
        self.path.write_text('{"version":"2.0","flags":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "feature flag state schema"):
            ProviderFeatureFlagManager(self.path)

        self.path.write_text(json.dumps({
            "version": "1.0",
            "flags": {
                "job_queue": {
                    "enabled": True, "rollout_percent": 100,
                    "allow_projects": ["film-a", "film-a"],
                    "deny_projects": [],
                }
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "feature flag record"):
            ProviderFeatureFlagManager(self.path)

    def test_flags_persist_and_invalid_values_fail_closed(self):
        self.manager.set_flag("new_router", enabled=True, rollout_percent=100)
        reloaded = ProviderFeatureFlagManager(self.path)
        self.assertTrue(reloaded.evaluate(
            "new_router", project_id="film", request_id="job-1"
        )["enabled"])
        self.assertEqual(self.manager.set_flag("bad flag", enabled=True)["status"], "INVALID_FLAG_NAME")
        self.assertEqual(self.manager.set_flag("flag", enabled=True, rollout_percent=101)["status"], "INVALID_ROLLOUT_PERCENT")


if __name__ == "__main__":
    unittest.main()
