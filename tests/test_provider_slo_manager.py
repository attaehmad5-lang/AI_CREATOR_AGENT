import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_metrics_manager import ProviderMetricsManager
from projects.provider_store.provider_slo_manager import ProviderSLOManager


class ProviderSLOManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "slo-policies.json"
        self.metrics = ProviderMetricsManager(max_samples=20)
        self.manager = ProviderSLOManager(self.metrics, self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_policy_and_insufficient_data_fail_closed(self):
        self.assertEqual(
            self.manager.evaluate(provider_id="image", category="image")["status"],
            "SLO_POLICY_NOT_FOUND",
        )
        self.manager.set_policy("image", "image", {
            "minimum_samples": 2, "min_success_rate": 0.9,
        })
        result = self.manager.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_DATA_INSUFFICIENT")
        self.assertIsNone(result["compliant"])

    def test_slo_breach_reports_each_violated_threshold(self):
        self.manager.set_policy("image", "image", {
            "minimum_samples": 3,
            "min_success_rate": 0.8,
            "max_p95_latency_ms": 500,
            "max_timeout_rate": 0.1,
            "max_avg_queue_wait_ms": 50,
        })
        self.metrics.record(
            provider_id="image", category="image", success=True,
            latency_ms=100, queue_wait_ms=10,
        )
        self.metrics.record(
            provider_id="image", category="image", success=True,
            latency_ms=200, queue_wait_ms=20,
        )
        self.metrics.record(
            provider_id="image", category="image", success=False,
            latency_ms=800, queue_wait_ms=200, timed_out=True,
        )
        result = self.manager.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_BREACH")
        self.assertFalse(result["compliant"])
        self.assertEqual(set(result["breaches"]), {
            "SUCCESS_RATE_BELOW_TARGET", "P95_LATENCY_ABOVE_TARGET",
            "TIMEOUT_RATE_ABOVE_TARGET", "QUEUE_WAIT_ABOVE_TARGET",
        })

    def test_compliant_slo_and_persisted_policy(self):
        policy = {"minimum_samples": 2, "min_success_rate": 0.9, "max_p95_latency_ms": 300}
        self.assertTrue(self.manager.set_policy("image", "image", policy)["saved"])
        self.metrics.record(
            provider_id="image", category="image", success=True,
            latency_ms=100, queue_wait_ms=5,
        )
        self.metrics.record(
            provider_id="image", category="image", success=True,
            latency_ms=150, queue_wait_ms=10,
        )
        reloaded = ProviderSLOManager(self.metrics, self.path)
        result = reloaded.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_COMPLIANT")
        self.assertTrue(result["compliant"])
        self.assertEqual(result["policy"], policy)

    def test_invalid_policy_values_are_rejected(self):
        for policy in (
            {"min_success_rate": 1.1},
            {"max_timeout_rate": -0.1},
            {"minimum_samples": True},
            {"max_p95_latency_ms": float("inf")},
            {"unexpected": True},
        ):
            result = self.manager.set_policy("image", "image", policy)
            self.assertEqual(result["status"], "INVALID_SLO_POLICY")
            self.assertFalse(result["saved"])

    def test_corrupt_policy_file_fails_closed(self):
        self.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderSLOManager(self.metrics, self.path)


if __name__ == "__main__":
    unittest.main()
