import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from projects.provider_store.metrics_manager import ProviderMetricsManager


def _record_metric_in_process(args):
    path, index = args
    manager = ProviderMetricsManager(path)
    return manager.record_request(
        request_id=f"process-req-{index}",
        trace_id=f"process-trace-{index}",
        provider_id="image-provider",
        modality="image",
        status="EXECUTION_SUCCEEDED",
        success=True,
        latency_ms=float(index),
    )["saved"]


class ProviderMetricsManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "metrics.json"
        self.now = [datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)]
        self.manager = ProviderMetricsManager(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.temporary.cleanup()

    def event(self, request_id="req-1", **overrides):
        record = {
            "request_id": request_id,
            "trace_id": "trace-1",
            "provider_id": "image-provider",
            "modality": "image",
            "status": "EXECUTION_SUCCEEDED",
            "success": True,
            "latency_ms": 125.5,
        }
        record.update(overrides)
        return record

    def test_records_safe_metrics_and_aggregates_success_latency(self):
        self.assertTrue(self.manager.record_request(**self.event(queue_wait_ms=10.0))["saved"])
        self.manager.record_request(**self.event(
            "req-2", trace_id="trace-2", status="EXECUTION_FAILED",
            success=False, latency_ms=250.0, queue_wait_ms=30.0,
        ))
        summary = self.manager.summary(provider_id="image-provider", modality="image")
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["average_latency_ms"], 187.75)
        self.assertEqual(summary["max_latency_ms"], 250.0)
        self.assertEqual(summary["queue_wait_sample_count"], 2)
        self.assertEqual(summary["average_queue_wait_ms"], 20.0)
        self.assertEqual(summary["max_queue_wait_ms"], 30.0)

    def test_throughput_queue_timeout_and_window_metrics(self):
        self.manager.record_request(**self.event(
            "queue-timeout-1", status="EXECUTION_QUEUE_TIMEOUT",
            success=False, latency_ms=50.0, queue_wait_ms=45.0,
        ))
        self.manager.record_request(**self.event(
            "completed-1", trace_id="trace-completed-1", queue_wait_ms=5.0,
        ))
        summary = self.manager.summary(window_seconds=10.0)
        self.assertEqual(summary["window_request_count"], 2)
        self.assertEqual(summary["queue_timeout_count"], 1)
        self.assertEqual(summary["capacity_rejected_count"], 0)
        self.assertEqual(summary["cancelled_count"], 0)
        self.assertEqual(summary["throughput_per_second"], 0.2)
        self.now[0] += timedelta(seconds=11)
        expired = self.manager.summary(window_seconds=10.0)
        self.assertEqual(expired["window_request_count"], 0)
        self.assertEqual(expired["throughput_per_second"], 0.0)
        self.assertEqual(self.manager.summary(window_seconds=0)["status"], "INVALID_METRICS_WINDOW")

    def test_duplicate_is_idempotent_and_conflicting_request_id_is_rejected(self):
        first = self.manager.record_request(**self.event())
        duplicate = self.manager.record_request(**self.event())
        conflict = self.manager.record_request(**self.event(status="EXECUTION_FAILED", success=False))
        self.assertEqual(first["status"], "METRIC_RECORDED")
        self.assertEqual(duplicate["status"], "ALREADY_RECORDED")
        self.assertTrue(duplicate["saved"])
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")
        self.assertFalse(conflict["saved"])
        self.assertEqual(self.manager.summary()["request_count"], 1)

    def test_concurrent_events_are_not_lost(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda i: self.manager.record_request(**self.event(
                    f"req-{i}", trace_id=f"trace-{i}", latency_ms=float(i)
                )),
                range(1, 61),
            ))
        self.assertTrue(all(item["saved"] for item in results))
        self.assertEqual(self.manager.summary()["request_count"], 60)

    def test_latency_aggregate_stays_finite_for_extreme_valid_values(self):
        self.manager.record_request(**self.event("huge-1", latency_ms=1e308))
        self.manager.record_request(**self.event(
            "huge-2", trace_id="trace-huge-2", latency_ms=1e308
        ))
        average = self.manager.summary()["average_latency_ms"]
        self.assertTrue(average < float("inf"))
        self.assertEqual(average, 1e308)

    def test_concurrent_processes_do_not_lose_metrics_events(self):
        args = [(str(self.path), index) for index in range(1, 17)]
        with ProcessPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_record_metric_in_process, args))
        self.assertTrue(all(results))
        self.assertEqual(ProviderMetricsManager(self.path).summary()["request_count"], 16)

    def test_retention_is_bounded_and_keeps_newest_events(self):
        manager = ProviderMetricsManager(
            self.path, max_events=3, clock=lambda: self.now[0]
        )
        for i in range(5):
            self.now[0] += timedelta(seconds=1)
            manager.record_request(**self.event(
                f"req-{i}", trace_id=f"trace-{i}", latency_ms=float(i)
            ))
        listed = manager.list_events(limit=10)
        self.assertEqual(len(listed["events"]), 3)
        self.assertEqual(
            {event["request_id"] for event in listed["events"]},
            {"req-2", "req-3", "req-4"},
        )

    def test_rejects_invalid_identifiers_and_nonfinite_latency(self):
        invalid = [
            ({"request_id": "../secret"}, "INVALID_REQUEST_ID"),
            ({"trace_id": "trace with spaces"}, "INVALID_TRACE_ID"),
            ({"provider_id": "provider\nsecret"}, "INVALID_PROVIDER_ID"),
            ({"modality": "Image"}, "INVALID_MODALITY"),
            ({"status": "ok"}, "INVALID_STATUS"),
            ({"success": 1}, "INVALID_SUCCESS_FLAG"),
            ({"latency_ms": float("inf")}, "INVALID_LATENCY"),
            ({"latency_ms": float("nan")}, "INVALID_LATENCY"),
            ({"queue_wait_ms": -1.0}, "INVALID_QUEUE_WAIT"),
            ({"queue_wait_ms": float("inf")}, "INVALID_QUEUE_WAIT"),
        ]
        for override, expected in invalid:
            with self.subTest(override=override):
                result = self.manager.record_request(**self.event(**override))
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["saved"])

    def test_event_storage_never_accepts_prompt_or_secret_fields(self):
        result = self.manager.record_request(**self.event())
        self.assertTrue(result["saved"])
        raw = self.path.read_text(encoding="utf-8")
        for forbidden in ("prompt", "output_path", "access_token", "api_key", "password", "secret"):
            self.assertNotIn(forbidden, raw.casefold())

    def test_persists_across_manager_instances(self):
        self.manager.record_request(**self.event())
        loaded = ProviderMetricsManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(loaded.summary()["request_count"], 1)
        self.assertEqual(loaded.list_events()["events"][0]["trace_id"], "trace-1")

    def test_corrupt_store_fails_closed_without_overwriting_it(self):
        self.path.write_text("{broken-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderMetricsManager(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken-json")

    def test_limits_and_clock_are_validated(self):
        for value in (0, -1, True, 100001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProviderMetricsManager(self.path, max_events=value)
        manager = ProviderMetricsManager(
            self.path, clock=lambda: datetime(2026, 10, 15, 12, 0)
        )
        result = manager.record_request(**self.event())
        self.assertEqual(result["status"], "INVALID_METRICS_CLOCK")

    def test_invalid_event_list_limit_is_rejected(self):
        self.assertEqual(self.manager.list_events(limit=0)["status"], "INVALID_LIMIT")
        self.assertEqual(self.manager.list_events(limit=True)["status"], "INVALID_LIMIT")


if __name__ == "__main__":
    unittest.main()
