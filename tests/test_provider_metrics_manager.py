"""Provider metrics aggregation and bounded-history tests."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

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

    def test_persistent_metrics_survive_restart_and_remain_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "metrics.json"
            first = ProviderMetricsManager(
                max_samples=2, clock=lambda: self.now, state_path=state_path
            )
            self.assertTrue(first.record(
                provider_id="image-a", category="image", success=True,
                latency_ms=10, queue_wait_ms=2,
            )["saved"])
            self.assertTrue(first.record(
                provider_id="image-a", category="image", success=False,
                latency_ms=30, queue_wait_ms=6, timed_out=True,
            )["saved"])
            self.assertTrue(first.record(
                provider_id="image-a", category="image", success=True,
                latency_ms=50, queue_wait_ms=4,
            )["saved"])

            restarted = ProviderMetricsManager(
                max_samples=2, clock=lambda: self.now, state_path=state_path
            )
            metrics = restarted.snapshot(provider_id="image-a")["metrics"]
            self.assertEqual(metrics["sample_count"], 2)
            self.assertEqual(metrics["success_count"], 1)
            self.assertEqual(metrics["failure_count"], 1)
            self.assertEqual(metrics["timeout_count"], 1)
            self.assertEqual(metrics["avg_latency_ms"], 40)
            self.assertEqual(metrics["avg_queue_wait_ms"], 5)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(set(state), {"version", "samples"})
            self.assertEqual(set(state["samples"][0]), {
                "provider_id", "category", "success", "latency_ms",
                "queue_wait_ms", "timed_out", "recorded_at",
            })

    def test_persistent_metrics_are_shared_across_concurrent_manager_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "metrics.json"
            managers = [
                ProviderMetricsManager(max_samples=100, clock=lambda: self.now, state_path=state_path)
                for _ in range(6)
            ]

            def write(index):
                return managers[index % len(managers)].record(
                    provider_id="image-a", category="image",
                    success=index % 2 == 0, latency_ms=index,
                    queue_wait_ms=index / 2, timed_out=index % 7 == 0,
                )

            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(write, range(48)))
            self.assertTrue(all(result["saved"] is True for result in results))
            snapshot = managers[0].snapshot(provider_id="image-a")
            self.assertEqual(snapshot["status"], "METRICS_AVAILABLE")
            self.assertEqual(snapshot["metrics"]["sample_count"], 48)
            self.assertEqual(snapshot["metrics"]["success_count"], 24)

    def test_persistent_metrics_reject_unapproved_payload_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "metrics.json"
            state_path.write_text(json.dumps({
                "version": 1,
                "samples": [{
                    "provider_id": "image-a",
                    "category": "image",
                    "success": True,
                    "latency_ms": 10,
                    "queue_wait_ms": 0,
                    "timed_out": False,
                    "recorded_at": self.now.isoformat(),
                    "prompt": "must never be persisted",
                }],
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                ProviderMetricsManager(state_path=state_path)


if __name__ == "__main__":
    unittest.main()
