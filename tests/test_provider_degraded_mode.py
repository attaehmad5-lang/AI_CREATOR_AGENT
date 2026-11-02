"""Degraded-mode transitions, hysteresis, persistence, and fail-closed tests."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager


class ProviderDegradedModeManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "degraded.json"
        self.now = [datetime(2026, 11, 2, 12, tzinfo=timezone.utc)]
        self.manager = ProviderDegradedModeManager(
            self.path, clock=lambda: self.now[0],
        )
        self.manager.configure(
            "image", normal_min_healthy_providers=2,
            degraded_min_healthy_providers=1,
            recovery_success_samples=3,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def statuses(**eligible):
        return [
            {"provider_id": provider_id, "eligible": is_eligible}
            for provider_id, is_eligible in eligible.items()
        ]

    def test_two_healthy_providers_produce_normal_mode(self):
        result = self.manager.evaluate(
            "image", self.statuses(primary=True, fallback=True),
        )
        self.assertEqual(result["status"], "PROVIDER_CATEGORY_NORMAL")
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["state"]["healthy_count"], 2)

    def test_one_healthy_provider_enters_degraded_mode(self):
        result = self.manager.evaluate(
            "image", self.statuses(primary=True, fallback=False),
        )
        self.assertEqual(result["status"], "PROVIDER_CATEGORY_DEGRADED")
        self.assertEqual(
            result["actions"],
            ["LIMIT_TO_HEALTHY_PROVIDERS", "REQUIRE_FRESH_HEALTH_EVIDENCE"],
        )
        self.assertEqual(result["state"]["healthy_provider_ids"], ["primary"])

    def test_zero_healthy_providers_enter_outage_and_block_new_execution(self):
        result = self.manager.evaluate(
            "image", self.statuses(primary=False, fallback=False),
        )
        self.assertEqual(result["status"], "PROVIDER_CATEGORY_OUTAGE")
        self.assertIn("BLOCK_NEW_PROVIDER_EXECUTION", result["actions"])
        self.assertIn("PRESERVE_STARTED_REQUEST_ACCOUNTING", result["actions"])

    def test_recovery_requires_consecutive_healthy_cycles(self):
        outage = self.manager.evaluate("image", self.statuses(primary=False, fallback=False))
        self.assertEqual(outage["status"], "PROVIDER_CATEGORY_OUTAGE")
        first = self.manager.evaluate("image", self.statuses(primary=True, fallback=True))
        self.assertEqual(first["status"], "PROVIDER_CATEGORY_OUTAGE")
        self.assertEqual(first["state"]["consecutive_healthy_cycles"], 1)
        second = self.manager.evaluate("image", self.statuses(primary=True, fallback=True))
        self.assertEqual(second["status"], "PROVIDER_CATEGORY_OUTAGE")
        third = self.manager.evaluate("image", self.statuses(primary=True, fallback=True))
        self.assertEqual(third["status"], "PROVIDER_CATEGORY_NORMAL")
        self.assertEqual(third["state"]["transition_count"], 2)

    def test_state_and_recovery_cursor_persist_across_restart(self):
        degraded = self.manager.evaluate(
            "image", self.statuses(primary=True, fallback=False),
        )
        self.assertEqual(degraded["status"], "PROVIDER_CATEGORY_DEGRADED")
        restarted = ProviderDegradedModeManager(
            self.path, clock=lambda: self.now[0],
        )
        self.assertEqual(restarted.get_state("image")["state"]["mode"], "DEGRADED")
        recovered = restarted.evaluate(
            "image", self.statuses(primary=True, fallback=True),
        )
        self.assertEqual(recovered["state"]["consecutive_healthy_cycles"], 1)

    def test_invalid_thresholds_and_provider_statuses_fail_closed(self):
        for kwargs in (
            {"normal_min_healthy_providers": True},
            {"normal_min_healthy_providers": 0},
            {"degraded_min_healthy_providers": 0},
            {"degraded_min_healthy_providers": 3},
            {"recovery_success_samples": False},
            {"recovery_success_samples": 101},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.manager.configure("invalid", **kwargs)["saved"])
        for values in (
            [],
            [{"provider_id": "a", "eligible": "yes"}],
            [{"provider_id": "a", "eligible": True}, {"provider_id": "a", "eligible": False}],
            [{"provider_id": "../escape", "eligible": True}],
            ["not-an-object"],
        ):
            with self.subTest(values=values):
                self.assertFalse(self.manager.evaluate("image", values)["evaluated"])

    def test_unknown_category_and_invalid_clock_do_not_claim_success(self):
        self.assertEqual(
            self.manager.evaluate("missing", self.statuses(a=True))["status"],
            "DEGRADED_MODE_POLICY_NOT_CONFIGURED",
        )
        naive = ProviderDegradedModeManager(
            Path(self.temp_dir.name) / "naive.json",
            clock=lambda: datetime(2026, 11, 2, 12),
        )
        naive.configure("image")
        self.assertEqual(
            naive.evaluate("image", self.statuses(a=True))["status"],
            "INVALID_CLOCK",
        )

    def test_malformed_persisted_policy_or_state_is_rejected(self):
        self.path.write_text(json.dumps({
            "version": "1.0",
            "policies": {
                "image": {
                    "normal_min_healthy_providers": True,
                    "degraded_min_healthy_providers": 1,
                    "recovery_success_samples": 3,
                }
            },
            "states": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "policy"):
            ProviderDegradedModeManager(self.path)


if __name__ == "__main__":
    unittest.main()
