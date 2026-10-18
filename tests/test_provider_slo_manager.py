import unittest

from projects.provider_store.provider_metrics_manager import ProviderMetricsManager
from projects.provider_store.provider_slo_manager import ProviderSLOManager


class ProviderSLOManagerTests(unittest.TestCase):
    def setUp(self):
        self.metrics = ProviderMetricsManager(max_samples=100)
        self.slo = ProviderSLOManager(self.metrics, {
            "image": {
                "target_success_rate": 0.9,
                "max_p95_latency_ms": 100,
                "min_samples": 2,
            },
            "category:voice": {
                "target_success_rate": 0.95,
                "max_p95_latency_ms": 250,
                "min_samples": 2,
            },
        })

    def record(self, success, latency):
        return self.metrics.record(
            provider_id="image", category="image", success=success,
            latency_ms=latency,
        )

    def test_slo_met_when_availability_and_p95_targets_pass(self):
        self.record(True, 20)
        self.record(True, 40)
        result = self.slo.evaluate("image", category="image")
        self.assertEqual(result["status"], "SLO_MET")
        self.assertTrue(result["slo_met"])
        self.assertEqual(result["availability"]["observed"], 1.0)
        self.assertEqual(result["latency"]["p95_observed_ms"], 40)
        self.assertEqual(result["error_budget_remaining_fraction"], 1.0)

    def test_availability_breach_reports_error_budget(self):
        self.record(True, 20)
        self.record(False, 30)
        result = self.slo.evaluate("image", category="image")
        self.assertEqual(result["status"], "SLO_BREACHED")
        self.assertFalse(result["availability"]["met"])
        self.assertEqual(result["error_budget_remaining_fraction"], 0.0)

    def test_latency_breach_is_reported_even_when_availability_passes(self):
        self.record(True, 20)
        self.record(True, 250)
        result = self.slo.evaluate("image", category="image")
        self.assertEqual(result["status"], "SLO_BREACHED")
        self.assertTrue(result["availability"]["met"])
        self.assertFalse(result["latency"]["met"])

    def test_insufficient_samples_do_not_claim_slo_success(self):
        self.record(True, 10)
        result = self.slo.evaluate("image", category="image")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertFalse(result["slo_met"])
        self.assertEqual(result["required_samples"], 2)

    def test_category_policy_fallback_is_supported(self):
        result = self.slo.evaluate("voice-provider", category="voice")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["policy"]["target_success_rate"], 0.95)

    def test_missing_policy_and_invalid_inputs_fail_closed(self):
        self.assertEqual(self.slo.evaluate("unknown")["status"], "SLO_POLICY_NOT_FOUND")
        self.assertEqual(self.slo.evaluate("")["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(self.slo.evaluate("image", category=" " )["status"], "INVALID_CATEGORY")

    def test_invalid_slo_policies_are_rejected(self):
        for policy in (
            {"target_success_rate": float("nan"), "max_p95_latency_ms": 100},
            {"target_success_rate": 1.1, "max_p95_latency_ms": 100},
            {"target_success_rate": 0.9, "max_p95_latency_ms": 0},
            {"target_success_rate": 0.9, "max_p95_latency_ms": 100, "min_samples": True},
            {"target_success_rate": 0.9, "max_p95_latency_ms": 100, "min_samples": 0},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                ProviderSLOManager(self.metrics, {"image": policy})

    def test_invalid_metrics_manager_and_unavailable_metrics_fail_safely(self):
        with self.assertRaises(ValueError):
            ProviderSLOManager(object(), {})
        class BrokenMetrics:
            def snapshot(self, **kwargs):
                raise OSError("private storage path")
        result = ProviderSLOManager(
            BrokenMetrics(), {"image": {
                "target_success_rate": 0.9, "max_p95_latency_ms": 100, "min_samples": 1,
            }}
        ).evaluate("image")
        self.assertEqual(result["status"], "METRICS_LOOKUP_FAILED")
        self.assertEqual(result["error_type"], "OSError")
        self.assertNotIn("private storage path", repr(result))


if __name__ == "__main__":
    unittest.main()
