"""Degraded-mode decisions fail closed when health evidence is missing or invalid."""
import unittest

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

    def test_boolean_minimum_is_not_accepted_as_integer(self):
        with self.assertRaisesRegex(ValueError, "minimum_healthy_providers"):
            ProviderDegradedModeManager(minimum_healthy_providers=True)


if __name__ == "__main__":
    unittest.main()
