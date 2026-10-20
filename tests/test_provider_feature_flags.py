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

    def test_rollout_decision_is_stable_across_manager_restarts(self):
        self.manager.set_flag("stable_router", enabled=True, rollout_percent=37.5)
        before = self.manager.evaluate(
            "stable_router", project_id="film", request_id="job-stable"
        )
        reloaded = ProviderFeatureFlagManager(self.path)
        after = reloaded.evaluate(
            "stable_router", project_id="film", request_id="job-stable"
        )
        self.assertEqual(before, after)

    def test_constructor_rejects_corrupt_state_without_overwriting_file(self):
        original = "{broken"
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid feature flag state JSON"):
            ProviderFeatureFlagManager(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_constructor_rejects_boolean_rollout_percent_in_persisted_state(self):
        malformed = {
            "version": "1.0",
            "flags": {
                "unsafe_flag": {
                    "enabled": True,
                    "rollout_percent": True,
                    "allow_projects": [],
                    "deny_projects": [],
                }
            },
        }
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid feature flag record"):
            ProviderFeatureFlagManager(self.path)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), malformed)

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
