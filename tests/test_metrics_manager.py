import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from projects.provider_store.metrics_manager import ProviderMetricsManager


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
        self.assertTrue(self.manager.record_request(**self.event())["saved"])
        self.manager.record_request(**self.event(
            "req-2", trace_id="trace-2", status="EXECUTION_FAILED",
            success=False, latency_ms=250.0,
        ))
        summary = self.manager.summary(provider_id="image-provider", modality="image")
        self.assertEqual(summary["request_count"], 2)
        self.assertEqual(summary["success_count"], 1)
        self.assertEqual(summary["failure_count"], 1)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["average_latency_ms"], 187.75)
        self.assertEqual(summary["max_latency_ms"], 250.0)

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
