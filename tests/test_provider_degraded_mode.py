"""Degraded-mode decisions fail closed when health evidence is missing or invalid."""
import unittest
from datetime import datetime, timezone

from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager


class ProviderDegradedModeTests(unittest.TestCase):
    def test_all_healthy_providers_yield_normal_mode(self):
        result = ProviderDegradedModeManager().evaluate({
            "image-a": "HEALTHY", "image-b": "HEALTHY",
        })
        self.assertEqual(result["status"], "DEGRADED_MODE_ASSESSED")
        self.assertEqual(result["mode"], "NORMAL")
        self.assertTrue(result["accepting_new_work"])
        self.assertEqual(result["unavailable_providers"], [])

    def test_partial_provider_outage_yields_degraded_mode(self):
        result = ProviderDegradedModeManager().evaluate({
            "image-a": "HEALTHY", "image-b": "UNHEALTHY",
            "voice-a": "UNKNOWN",
        })
        self.assertEqual(result["mode"], "DEGRADED")
        self.assertTrue(result["accepting_new_work"])
        self.assertEqual(result["healthy_providers"], ["image-a"])
        self.assertEqual(result["unavailable_providers"], ["image-b", "voice-a"])
        self.assertEqual(result["reason_codes"], ["PARTIAL_PROVIDER_UNAVAILABILITY"])

    def test_no_healthy_provider_fails_closed_as_outage(self):
        result = ProviderDegradedModeManager().evaluate({
            "image-a": "UNHEALTHY", "voice-a": "UNKNOWN",
        })
        self.assertEqual(result["mode"], "OUTAGE")
        self.assertFalse(result["accepting_new_work"])
        self.assertEqual(result["reason_codes"], ["NO_HEALTHY_PROVIDERS"])

    def test_minimum_healthy_capacity_is_enforced(self):
        manager = ProviderDegradedModeManager(minimum_healthy_providers=2)
        result = manager.evaluate({
            "image-a": "HEALTHY", "image-b": "UNHEALTHY",
        })
        self.assertEqual(result["mode"], "OUTAGE")
        self.assertFalse(result["accepting_new_work"])
        self.assertEqual(result["reason_codes"], ["HEALTHY_CAPACITY_BELOW_MINIMUM"])

    def test_missing_empty_or_malformed_evidence_fails_closed(self):
        manager = ProviderDegradedModeManager()
        for states in (None, {}, [], {"bad id": "HEALTHY"}, {"image": "maybe"}):
            with self.subTest(states=states):
                result = manager.evaluate(states)
                self.assertEqual(result["status"], "INVALID_PROVIDER_STATES")
                self.assertEqual(result["mode"], "OUTAGE")
                self.assertFalse(result["accepting_new_work"])

    def test_stale_or_future_timestamped_health_is_not_treated_as_healthy(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = ProviderDegradedModeManager().evaluate(
            {
                "fresh": {"status": "HEALTHY", "checked_at": "2026-01-01T11:59:30+00:00"},
                "stale": {"status": "HEALTHY", "checked_at": "2026-01-01T11:00:00+00:00"},
                "future": {"status": "HEALTHY", "checked_at": "2026-01-01T12:01:00+00:00"},
            },
            max_age_seconds=60,
            now=now,
        )
        self.assertEqual(result["mode"], "DEGRADED")
        self.assertEqual(result["healthy_providers"], ["fresh"])
        self.assertEqual(result["unavailable_providers"], ["future", "stale"])

    def test_freshness_policy_requires_timestamps_and_rejects_invalid_age(self):
        manager = ProviderDegradedModeManager()
        missing_timestamp = manager.evaluate(
            {"image": "HEALTHY"}, max_age_seconds=60,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(missing_timestamp["mode"], "OUTAGE")
        self.assertEqual(
            missing_timestamp["reason_codes"],
            ["TIMESTAMPED_HEALTH_EVIDENCE_REQUIRED"],
        )
        invalid_age = manager.evaluate({"image": "HEALTHY"}, max_age_seconds=float("nan"))
        self.assertEqual(invalid_age["status"], "INVALID_HEALTH_POLICY")
        self.assertFalse(invalid_age["accepting_new_work"])

    def test_boolean_minimum_is_not_accepted_as_integer(self):
        with self.assertRaisesRegex(ValueError, "minimum_healthy_providers"):
            ProviderDegradedModeManager(minimum_healthy_providers=True)


if __name__ == "__main__":
    unittest.main()
