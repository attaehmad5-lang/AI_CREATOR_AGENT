"""Provider/model benchmark aggregation, confidence and persistence tests."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_benchmark_manager import ProviderBenchmarkManager


class ProviderBenchmarkManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        self.manager = ProviderBenchmarkManager(
            max_samples=10, clock=lambda: self.now,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def add(self, provider, model, success, latency, cost, quality, manager=None):
        selected = manager or self.manager
        return selected.record(
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
        self.assertAlmostEqual(a["avg_cost_per_attempt"], 0.15)
        self.assertEqual(a["avg_cost_per_success"], 0.2)
        self.assertEqual(a["avg_quality_score"], 0.9)
        self.assertEqual(a["quality_sample_count"], 1)
        self.assertEqual(a["sample_confidence"], "INSUFFICIENT_SAMPLES")
        self.assertEqual(result["results"][0]["provider_id"], "provider-b")
        interval = a["success_rate_ci_95"]
        self.assertGreaterEqual(interval["lower"], 0.0)
        self.assertLessEqual(interval["upper"], 1.0)
        self.assertLess(interval["lower"], interval["upper"])

    def test_history_is_bounded(self):
        manager = ProviderBenchmarkManager(max_samples=2, clock=lambda: self.now)
        for latency in (10, 20, 30):
            manager.record(
                provider_id="p", model_id="m", modality="image", run_id="r",
                success=True, latency_ms=latency, actual_cost=0.0,
            )
        result = manager.compare()["results"][0]
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["p50_latency_ms"], 20)

    def test_invalid_samples_and_timestamps_fail_closed(self):
        self.assertEqual(self.manager.record(
            provider_id="image", model_id="m", modality="image", run_id="r",
            success=True, latency_ms=float("nan"), actual_cost=0.1,
        )["status"], "INVALID_BENCHMARK_METRIC")
        self.assertEqual(self.manager.compare()["status"], "NO_BENCHMARKS")
        naive_clock = ProviderBenchmarkManager(clock=lambda: datetime(2026, 11, 6, 12))
        self.assertEqual(naive_clock.record(
            provider_id="p", model_id="m", modality="image", run_id="r",
            success=True, latency_ms=1, actual_cost=0,
        )["status"], "INVALID_BENCHMARK_TIMESTAMP")

    def test_file_backed_history_survives_restart_and_is_bounded(self):
        path = self.root / "benchmarks.json"
        manager = ProviderBenchmarkManager(
            max_samples=2, storage_path=path, clock=lambda: self.now,
        )
        for latency in (10, 20, 30):
            self.add("provider-a", "model-1", True, latency, 0.1, 0.8, manager)
        restarted = ProviderBenchmarkManager(
            max_samples=2, storage_path=path, clock=lambda: self.now,
        )
        result = restarted.compare()["results"][0]
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["p50_latency_ms"], 20)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["samples"]), 2)
        self.assertIn("recorded_at", payload["samples"][0])

    def test_concurrent_instances_do_not_lose_persisted_samples(self):
        path = self.root / "shared-benchmarks.json"
        managers = [
            ProviderBenchmarkManager(
                max_samples=50, storage_path=path, clock=lambda: self.now,
            )
            for _ in range(4)
        ]
        def write(index):
            manager = managers[index % len(managers)]
            return manager.record(
                provider_id=f"provider-{index % 2}", model_id="model-1",
                modality="image", run_id=f"run-{index}", success=True,
                latency_ms=index + 1, actual_cost=0.0, quality_score=0.8,
            )
        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(write, range(40)))
        self.assertTrue(all(item["saved"] for item in outcomes))
        result = ProviderBenchmarkManager(
            max_samples=50, storage_path=path, clock=lambda: self.now,
        ).compare()
        self.assertEqual(sum(item["sample_count"] for item in result["results"]), 40)

    def test_corrupt_persisted_state_is_not_overwritten(self):
        path = self.root / "corrupt-benchmarks.json"
        original = b'{"schema_version":1,"schema_version":1,"samples":[]}'
        path.write_bytes(original)
        manager = ProviderBenchmarkManager(storage_path=path, clock=lambda: self.now)
        rejected = self.add("p", "m", True, 10, 0.0, None, manager)
        self.assertEqual(rejected["status"], "BENCHMARK_STATE_INVALID")
        self.assertFalse(rejected["saved"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(manager.compare()["status"], "BENCHMARK_STATE_INVALID")

    def test_unknown_fields_and_invalid_timestamps_are_rejected(self):
        path = self.root / "invalid-benchmarks.json"
        manager = ProviderBenchmarkManager(storage_path=path, clock=lambda: self.now)
        self.add("p", "m", True, 10, 0.0, None, manager)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["samples"][0]["prompt"] = "must-not-persist"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(manager.compare()["status"], "BENCHMARK_STATE_INVALID")
        self.assertFalse(manager.record(
            provider_id="p", model_id="m", modality="image", run_id="r",
            success=True, latency_ms=1, actual_cost=0.0,
        )["saved"])

    def test_confidence_sample_threshold_is_configurable(self):
        manager = ProviderBenchmarkManager(
            minimum_samples_for_confidence=2, clock=lambda: self.now,
        )
        self.add("p", "m", True, 10, 0, 0.9, manager)
        self.add("p", "m", True, 12, 0, 0.9, manager)
        self.assertEqual(
            manager.compare()["results"][0]["sample_confidence"],
            "MEETS_MINIMUM_SAMPLE_COUNT",
        )

    def test_invalid_configuration_is_rejected(self):
        for kwargs in (
            {"max_samples": 0},
            {"max_samples": True},
            {"minimum_samples_for_confidence": 0},
            {"minimum_samples_for_confidence": True},
            {"storage_path": ""},
            {"clock": "not-callable"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ProviderBenchmarkManager(**kwargs)


if __name__ == "__main__":
    unittest.main()
