import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_metrics import ProviderMetricsManager
from projects.provider_store.provider_service import ProviderService


class _Connection:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class ProviderMetricsManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_50_case_metrics_matrix_counts_latency_queue_and_timeouts(self):
        manager = ProviderMetricsManager(max_events=100)
        for index in range(1, 51):
            timed_out = index % 10 == 0
            success = index % 2 == 0 and not timed_out
            result = manager.record_request(
                "image-provider",
                success=success,
                latency_ms=float(index * 10),
                queue_wait_ms=float(index),
                timed_out=timed_out,
                request_id=f"request-{index}",
                timestamp=950.0 + index,
            )
            with self.subTest(case=index):
                self.assertEqual(result["status"], "RECORDED")
        snapshot = manager.snapshot("image-provider", window_seconds=300, now=1000.0)
        self.assertEqual(snapshot["request_count"], 50)
        self.assertEqual(snapshot["success_count"], 20)
        self.assertEqual(snapshot["failure_count"], 30)
        self.assertEqual(snapshot["timeout_count"], 5)
        self.assertEqual(snapshot["success_rate_pct"], 40.0)
        self.assertEqual(snapshot["average_latency_ms"], 255.0)
        self.assertEqual(snapshot["p95_latency_ms"], 480.0)
        self.assertEqual(snapshot["average_queue_wait_ms"], 25.5)
        self.assertEqual(snapshot["throughput_per_minute"], 10.0)

    def test_duplicate_request_id_is_idempotent(self):
        manager = ProviderMetricsManager()
        options = dict(
            provider_id="p", success=True, latency_ms=10.0,
            request_id="job-1", timestamp=1000.0,
        )
        self.assertEqual(manager.record_request(**options)["status"], "RECORDED")
        self.assertEqual(manager.record_request(**options)["status"], "DUPLICATE_METRIC_EVENT")
        self.assertEqual(manager.snapshot("p", now=1000.0)["request_count"], 1)

    def test_persisted_metrics_survive_reload_and_are_bounded(self):
        path = self.root / "metrics.json"
        manager = ProviderMetricsManager(path, max_events=3)
        for index in range(5):
            manager.record_request(
                "p", success=index % 2 == 0, latency_ms=float(index + 1),
                queue_wait_ms=float(index), request_id=f"job-{index}", timestamp=1000 + index,
            )
        reloaded = ProviderMetricsManager(path, max_events=3)
        events = reloaded.list_events("p")["events"]
        self.assertEqual(len(events), 3)
        self.assertEqual([event["request_id"] for event in events], ["job-2", "job-3", "job-4"])
        self.assertEqual(reloaded.snapshot("p", now=1004)["request_count"], 3)

    def test_snapshot_respects_window_and_ignores_future_events(self):
        manager = ProviderMetricsManager()
        manager.record_request("p", success=True, latency_ms=10, request_id="old", timestamp=100)
        manager.record_request("p", success=False, latency_ms=20, request_id="current", timestamp=995)
        manager.record_request("p", success=True, latency_ms=30, request_id="future", timestamp=1001)
        result = manager.snapshot("p", window_seconds=10, now=1000)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["average_latency_ms"], 20.0)

    def test_missing_latency_is_counted_without_fabricating_latency(self):
        manager = ProviderMetricsManager()
        manager.record_request("p", success=True, latency_ms=None, request_id="no-latency", timestamp=1000)
        result = manager.snapshot("p", now=1000)
        self.assertEqual(result["request_count"], 1)
        self.assertIsNone(result["average_latency_ms"])
        self.assertIsNone(result["p95_latency_ms"])

    def test_service_records_timeout_latency_and_queue_wait(self):
        manager = ProviderMetricsManager()
        service = ProviderService(_Connection(), metrics_manager=manager)
        result = service.finalize_started_request(
            project_id="film", provider_id="image", request_id="timeout-job",
            estimated_cost=0.0, actual_cost=0.0, success=False,
            error_code="TIMEOUT", latency_ms=120.0, queue_wait_ms=35.0,
        )
        self.assertTrue(result["finalized"])
        self.assertEqual(result["metrics"]["status"], "RECORDED")
        snapshot = manager.snapshot("image")
        self.assertEqual(snapshot["timeout_count"], 1)
        self.assertEqual(snapshot["average_latency_ms"], 120.0)
        self.assertEqual(snapshot["average_queue_wait_ms"], 35.0)

    def test_invalid_inputs_fail_closed(self):
        manager = ProviderMetricsManager()
        self.assertEqual(manager.record_request("", success=True, latency_ms=1)["status"], "INVALID_PROVIDER")
        self.assertEqual(manager.record_request("p", success=True, latency_ms=float("nan"))["status"], "INVALID_METRIC_VALUE")
        self.assertEqual(manager.record_request("p", success=True, latency_ms=1, timed_out=True)["status"], "INVALID_OUTCOME")
        self.assertEqual(manager.record_request("p", success=False, latency_ms=1, request_id="")["status"], "INVALID_REQUEST_ID")
        self.assertEqual(manager.snapshot("p", window_seconds=0)["status"], "INVALID_WINDOW")
        self.assertEqual(manager.snapshot("p", now=float("inf"))["status"], "INVALID_CLOCK")
        self.assertEqual(manager.list_events("")["status"], "INVALID_PROVIDER")

    def test_corrupt_metrics_ledger_fails_closed(self):
        path = self.root / "corrupt.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderMetricsManager(path)

    def test_metrics_persistence_failure_does_not_keep_uncommitted_event(self):
        path = self.root / "metrics-failure.json"
        manager = ProviderMetricsManager(path)
        # A directory at the destination forces atomic replacement to fail.
        path.mkdir()
        result = manager.record_request(
            "p", success=True, latency_ms=1.0, request_id="failed-write", timestamp=1000
        )
        self.assertEqual(result["status"], "METRICS_PERSISTENCE_FAILED")
        self.assertEqual(manager.list_events("p")["events"], [])


if __name__ == "__main__":
    unittest.main()
