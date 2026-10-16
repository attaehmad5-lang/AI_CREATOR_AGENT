import unittest
from datetime import datetime, timezone

from projects.provider_store.provider_metrics_manager import ProviderMetricsManager
from projects.provider_store.provider_slo_manager import ProviderSLOManager


class ProviderSLOManagerTests(unittest.TestCase):
    def setUp(self):
        self.metrics = ProviderMetricsManager(max_samples=100)
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manager = ProviderSLOManager(
            self.metrics, max_history=2, clock=lambda: self.now
        )

    def test_insufficient_samples_do_not_claim_slo_pass(self):
        self.manager.set_target(
            provider_id="image", category="image", min_samples=3,
            min_success_rate=0.9,
        )
        self.metrics.record(
            provider_id="image", category="image", success=True, latency_ms=10
        )
        result = self.manager.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_INSUFFICIENT_DATA")
        self.assertFalse(result["evaluated"])

    def test_slo_passes_when_all_configured_targets_are_met(self):
        self.manager.set_target(
            provider_id="image", category="image", min_samples=2,
            min_success_rate=0.9, max_p95_latency_ms=100,
            max_timeout_rate=0.1,
        )
        for latency in (20, 30, 40):
            self.metrics.record(
                provider_id="image", category="image",
                success=True, latency_ms=latency,
            )
        result = self.manager.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_MET")
        self.assertEqual(result["breached_objectives"], [])
        self.assertEqual(result["sample_count"], 3)

    def test_slo_breach_reports_only_objective_names(self):
        self.manager.set_target(
            provider_id="image", category="image", min_samples=2,
            min_success_rate=0.9, max_p95_latency_ms=50,
            max_timeout_rate=0.1,
        )
        self.metrics.record(
            provider_id="image", category="image",
            success=False, latency_ms=200, timed_out=True,
        )
        self.metrics.record(
            provider_id="image", category="image",
            success=True, latency_ms=300,
        )
        result = self.manager.evaluate(provider_id="image", category="image")
        self.assertEqual(result["status"], "SLO_BREACHED")
        self.assertEqual(
            set(result["breached_objectives"]),
            {"SUCCESS_RATE", "TIMEOUT_RATE", "P95_LATENCY"},
        )
        self.assertNotIn("payload", repr(result))
        self.assertEqual(len(self.manager.history()), 1)

    def test_invalid_targets_and_missing_target_fail_closed(self):
        self.assertEqual(
            self.manager.set_target(
                provider_id="image", category="image", min_success_rate=2
            )["status"],
            "INVALID_SUCCESS_RATE",
        )
        self.assertEqual(
            self.manager.evaluate(provider_id="image", category="image")["status"],
            "SLO_TARGET_NOT_FOUND",
        )

    def test_slo_history_is_bounded(self):
        self.manager.set_target(
            provider_id="image", category="image", min_samples=1
        )
        for latency in (10, 20, 30):
            self.metrics.record(
                provider_id="image", category="image",
                success=True, latency_ms=latency,
            )
            self.manager.evaluate(provider_id="image", category="image")
        history = self.manager.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["evaluated_at"], self.now.isoformat())


if __name__ == "__main__":
    unittest.main()
