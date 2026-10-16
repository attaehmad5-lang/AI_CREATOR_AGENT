"""Provider metrics aggregation and bounded-history tests."""
import unittest
from datetime import datetime, timezone

from projects.provider_store.provider_metrics_manager import ProviderMetricsManager


class ProviderMetricsManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manager = ProviderMetricsManager(max_samples=3, clock=lambda: self.now)

    def record(self, provider, success, latency, wait=0, timeout=False):
        return self.manager.record(
            provider_id=provider, category="image", success=success,
            latency_ms=latency, queue_wait_ms=wait, timed_out=timeout,
        )

    def test_metrics_aggregate_success_failure_timeout_latency_and_queue_wait(self):
        self.record("image-a", True, 10, wait=2)
        self.record("image-a", False, 30, wait=6, timeout=True)
        self.record("image-b", True, 20, wait=4)
        result = self.manager.snapshot(provider_id="image-a")
        metrics = result["metrics"]
        self.assertEqual(result["status"], "METRICS_AVAILABLE")
        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["success_count"], 1)
        self.assertEqual(metrics["failure_count"], 1)
        self.assertEqual(metrics["timeout_count"], 1)
        self.assertEqual(metrics["success_rate"], 0.5)
        self.assertEqual(metrics["avg_latency_ms"], 20)
        self.assertEqual(metrics["p50_latency_ms"], 10)
        self.assertEqual(metrics["p95_latency_ms"], 30)
        self.assertEqual(metrics["avg_queue_wait_ms"], 4)

    def test_sample_history_is_bounded(self):
        for value in (1, 2, 3, 4):
            self.record("image-a", True, value)
        result = self.manager.snapshot(provider_id="image-a")
        self.assertEqual(result["metrics"]["sample_count"], 3)
        self.assertEqual(result["metrics"]["avg_latency_ms"], 3)

    def test_empty_metrics_and_invalid_inputs_are_safe(self):
        self.assertEqual(self.manager.snapshot()["status"], "NO_METRICS")
        self.assertEqual(self.record("image-a", True, float("nan"))["status"], "INVALID_METRIC_TIMING")
        self.assertEqual(self.manager.record(
            provider_id="image-a", category="image", success="yes",
            latency_ms=1,
        )["status"], "INVALID_METRIC_FLAGS")

    def test_concurrent_records_preserve_sample_bound(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: self.record("image-a", i % 2 == 0, i), range(100)))
        self.assertEqual(self.manager.snapshot(provider_id="image-a")["metrics"]["sample_count"], 3)


if __name__ == "__main__":
    unittest.main()
