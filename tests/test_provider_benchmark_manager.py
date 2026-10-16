"""Provider/model benchmark aggregation tests."""
import unittest

from projects.provider_store.provider_benchmark_manager import ProviderBenchmarkManager


class ProviderBenchmarkManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = ProviderBenchmarkManager(max_samples=10)

    def add(self, provider, model, success, latency, cost, quality):
        return self.manager.record(
            provider_id=provider, model_id=model, modality="image",
            run_id="benchmark-run-1", success=success, latency_ms=latency,
            actual_cost=cost, quality_score=quality,
        )

    def test_comparison_aggregates_quality_latency_success_and_cost(self):
        self.add("provider-a", "model-1", True, 100, 0.20, 0.9)
        self.add("provider-a", "model-1", False, 300, 0.10, None)
        self.add("provider-b", "model-2", True, 80, 0.25, 0.8)
        result = self.manager.compare()
        self.assertEqual(result["status"], "BENCHMARKS_AVAILABLE")
        a = next(x for x in result["results"] if x["provider_id"] == "provider-a")
        self.assertEqual(a["sample_count"], 2)
        self.assertEqual(a["success_rate"], 0.5)
        self.assertEqual(a["p50_latency_ms"], 100)
        self.assertEqual(a["p95_latency_ms"], 300)
        self.assertEqual(a["avg_cost_per_attempt"], 0.15)
        self.assertEqual(a["avg_cost_per_success"], 0.2)
        self.assertEqual(a["avg_quality_score"], 0.9)
        self.assertEqual(result["results"][0]["provider_id"], "provider-b")

    def test_invalid_samples_fail_closed(self):
        self.assertEqual(self.manager.record(
            provider_id="image", model_id="m", modality="image", run_id="r",
            success=True, latency_ms=float("nan"), actual_cost=0.1,
        )["status"], "INVALID_BENCHMARK_METRIC")
        self.assertEqual(self.manager.compare()["status"], "NO_BENCHMARKS")

    def test_history_is_bounded(self):
        manager = ProviderBenchmarkManager(max_samples=2)
        for latency in (10, 20, 30):
            manager.record(provider_id="p", model_id="m", modality="image",
                           run_id="r", success=True, latency_ms=latency, actual_cost=0.0)
        result = manager.compare()["results"][0]
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["p50_latency_ms"], 20)


if __name__ == "__main__":
    unittest.main()
