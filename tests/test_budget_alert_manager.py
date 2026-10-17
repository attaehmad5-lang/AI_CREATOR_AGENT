"""Tests for persistent budget alert threshold events."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        self.assertEqual(first["alerts"], [])
        second = self.manager.record(
            project_id="film", spend=8.5, budget=10.0, request_id="job-2"
        )
        self.assertEqual([x["threshold"] for x in second["alerts"]], [0.5, 0.8])
        reloaded = BudgetAlertManager(self.path, clock=lambda: self.now)
        self.assertEqual(len(reloaded.pending_alerts()["alerts"]), 2)
        self.assertEqual(len(reloaded.data["events"]), 2)

    def test_budget_alerts_are_idempotent_per_period_but_repeat_next_period(self):
        first = self.manager.record(
            project_id="film", spend=9.0, budget=10.0,
            request_id="job-oct-1", budget_period_id="2026-10",
        )
        duplicate = self.manager.record(
            project_id="film", spend=9.5, budget=10.0,
            request_id="job-oct-2", budget_period_id="2026-10",
        )
        next_period = self.manager.record(
            project_id="film", spend=8.0, budget=10.0,
            request_id="job-nov-1", budget_period_id="2026-11",
        )
        self.assertEqual(len(first["alerts"]), 2)
        self.assertEqual(len(duplicate["alerts"]), 2)
        self.assertEqual(
            {event["event_id"] for event in first["alerts"]},
            {event["event_id"] for event in duplicate["alerts"]},
        )
        self.assertEqual(len(next_period["alerts"]), 2)
        self.assertTrue(all(
            event["budget_period_id"] == "2026-11"
            for event in next_period["alerts"]
        ))
        self.assertEqual(len(self.manager.data["events"]), 4)

    def test_invalid_budget_period_id_fails_closed(self):
        result = self.manager.record(
            project_id="film", spend=9.0, budget=10.0,
            request_id="job-1", budget_period_id="bad\nperiod",
        )
        self.assertEqual(result["status"], "INVALID_BUDGET_PERIOD_ID")
        self.assertEqual(result["alerts"], [])

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

    def test_dispatch_pending_delivers_events_and_marks_them_idempotently(self):
        self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-dispatch"
        )
        seen = []
        result = self.manager.dispatch_pending(
            lambda alert: seen.append(alert["event_id"]) or True,
            limit=10,
        )
        self.assertEqual(result["status"], "DELIVERY_ATTEMPTS_COMPLETED")
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.manager.pending_alerts()["alerts"], [])

    def test_failed_delivery_remains_pending_and_can_be_retried_without_leaking_error(self):
        self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-retry"
        )
        failed = self.manager.dispatch_pending(
            lambda alert: (_ for _ in ()).throw(RuntimeError("private webhook response")),
            limit=1,
        )
        self.assertEqual(failed["failed"], 1)
        pending = self.manager.pending_alerts(limit=1)["alerts"][0]
        self.assertEqual(pending["delivery_attempts"], 1)
        self.assertEqual(pending["last_delivery_error_code"], "DELIVERY_CALLBACK_FAILED")
        self.assertNotIn("private webhook response", repr(pending))
        retried = self.manager.dispatch_pending(lambda alert: {"delivered": True}, limit=10)
        self.assertEqual(retried["delivered"], 2)
        self.assertEqual(len(self.manager.pending_alerts()["alerts"]), 0)

    def test_concurrent_dispatchers_do_not_claim_the_same_alert(self):
        manager = BudgetAlertManager(
            self.path, thresholds=(1.0,), clock=lambda: self.now,
        )
        manager.record(
            project_id="film", spend=10.0, budget=10.0, request_id="job-once"
        )
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def sender(alert):
            calls.append(alert["event_id"])
            entered.set()
            release.wait(timeout=3)
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(manager.dispatch_pending, sender, limit=1)
            self.assertTrue(entered.wait(timeout=2))
            second = manager.dispatch_pending(lambda alert: calls.append(alert["event_id"]) or True, limit=1)
            self.assertEqual(second["attempted"], 0)
            release.set()
            result = first.result(timeout=3)

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manager.pending_alerts()["alerts"], [])

    def test_expired_delivery_lease_can_be_reclaimed(self):
        manager = BudgetAlertManager(
            self.path, thresholds=(1.0,), delivery_lease_seconds=5,
            clock=lambda: self.now,
        )
        manager.record(
            project_id="film", spend=10.0, budget=10.0, request_id="job-lease"
        )
        claimed = manager._claim_pending_alerts(limit=1)
        self.assertEqual(claimed[0]["status"], "DELIVERING")
        self.now = self.now.replace(minute=13)
        result = manager.dispatch_pending(lambda alert: True, limit=1)
        self.assertEqual(result["delivered"], 1)

    def test_dispatch_rejects_invalid_callback_and_limit(self):
        self.assertEqual(self.manager.dispatch_pending(None)["status"], "INVALID_DELIVERY_CALLBACK")
        self.assertEqual(self.manager.dispatch_pending(lambda _: True, limit=0)["status"], "INVALID_LIMIT")

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

    def test_corrupt_delivery_metadata_and_nonfinite_amounts_are_rejected(self):
        self.path.write_text(json.dumps({
            "version": "1.0",
            "events": {
                "event": {
                    "event_id": "event", "project_id": "film", "threshold": 0.8,
                    "budget": 10.0, "spend": 9.0, "ratio": 0.9,
                    "request_id": "job-1", "status": "PENDING",
                    "created_at": self.now.isoformat(), "delivery_attempts": "many",
                }
            },
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now)
        self.path.write_text(json.dumps({
            "version": "1.0",
            "events": {
                "event": {
                    "event_id": "event", "project_id": "film", "threshold": 0.8,
                    "budget": 10.0, "spend": 9.0, "ratio": float("inf"),
                    "request_id": "job-1", "status": "PENDING",
                    "created_at": self.now.isoformat(),
                }
            },
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now)

    def test_corrupt_state_is_rejected(self):
        self.path.write_text(json.dumps({"events": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now)


if __name__ == "__main__":
    unittest.main()
