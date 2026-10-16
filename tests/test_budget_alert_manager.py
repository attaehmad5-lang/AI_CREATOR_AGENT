"""Tests for persistent budget alert threshold events."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.budget_alert_manager import BudgetAlertManager


class BudgetAlertManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "alerts.json"
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manager = BudgetAlertManager(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_thresholds_queue_once_and_persist_across_reload(self):
        first = self.manager.record(
            project_id="film", spend=4.0, budget=10.0, request_id="job-1"
        )
        self.assertEqual(first["status"], "NO_BUDGET_ALERT")
        self.assertEqual([x["threshold"] for x in first["alerts"]], [0.5])
        second = self.manager.record(
            project_id="film", spend=8.5, budget=10.0, request_id="job-2"
        )
        self.assertEqual([x["threshold"] for x in second["alerts"]], [0.5, 0.8])
        reloaded = BudgetAlertManager(self.path, clock=lambda: self.now)
        self.assertEqual(len(reloaded.pending_alerts()["alerts"]), 2)
        self.assertEqual(len(reloaded.data["events"]), 2)

    def test_exactly_at_hard_stop_threshold_emits_100_percent_alert(self):
        result = self.manager.record(
            project_id="film", spend=10.0, budget=10.0, request_id="job-1"
        )
        self.assertEqual(
            [x["threshold"] for x in result["alerts"]], [0.5, 0.8, 1.0]
        )
        self.assertEqual(result["status"], "BUDGET_ALERTS_QUEUED")

    def test_alert_spend_only_moves_forward_and_delivery_is_idempotent(self):
        result = self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-1"
        )
        event_id = result["alerts"][0]["event_id"]
        lower = self.manager.record(
            project_id="film", spend=6.0, budget=10.0, request_id="job-2"
        )
        event = next(x for x in lower["alerts"] if x["event_id"] == event_id)
        self.assertEqual(event["spend"], 9.0)
        self.assertEqual(self.manager.mark_delivered(event_id)["status"], "ALERT_DELIVERED")
        self.assertEqual(self.manager.mark_delivered(event_id)["status"], "ALREADY_DELIVERED")
        self.assertEqual(len(self.manager.pending_alerts()["alerts"]), 1)

    def test_invalid_inputs_fail_closed(self):
        cases = [
            {"project_id": "", "spend": 1, "budget": 2, "request_id": "r"},
            {"project_id": "p", "spend": -1, "budget": 2, "request_id": "r"},
            {"project_id": "p", "spend": 1, "budget": 0, "request_id": "r"},
            {"project_id": "p", "spend": float("nan"), "budget": 2, "request_id": "r"},
            {"project_id": "p", "spend": 1, "budget": 2, "request_id": ""},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.assertTrue(self.manager.record(**kwargs)["status"].startswith("INVALID_"))

    def test_pending_limit_is_bounded(self):
        self.assertEqual(self.manager.pending_alerts(limit=0)["status"], "INVALID_LIMIT")
        self.assertEqual(self.manager.pending_alerts(limit=1001)["status"], "INVALID_LIMIT")

    def test_corrupt_state_is_rejected(self):
        self.path.write_text(json.dumps({"events": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now)


if __name__ == "__main__":
    unittest.main()
