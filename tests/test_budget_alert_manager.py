"""Tests for durable, idempotent budget alerting and bounded delivery."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.budget_alert_manager import BudgetAlertManager


class BudgetAlertManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "alerts.json"
        self.now = [datetime(2026, 10, 16, 12, tzinfo=timezone.utc)]
        self.manager = BudgetAlertManager(
            self.path, clock=lambda: self.now[0],
            retry_base_seconds=30, retry_max_seconds=300,
        )

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
        reloaded = BudgetAlertManager(self.path, clock=lambda: self.now[0])
        self.assertEqual(len(reloaded.pending_alerts()["alerts"]), 2)
        self.assertEqual(len(reloaded.data["events"]), 2)

    def test_exactly_at_hard_stop_threshold_emits_100_percent_alert(self):
        result = self.manager.record(
            project_id="film", spend=10.0, budget=10.0, request_id="job-1"
        )
        self.assertEqual([x["threshold"] for x in result["alerts"]], [0.5, 0.8, 1.0])
        self.assertEqual(result["status"], "BUDGET_ALERTS_QUEUED")

    def test_budget_period_id_creates_one_idempotent_alert_set_per_cycle(self):
        first = self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="monthly-1",
            budget_period_id="2026-10",
        )
        repeat = self.manager.record(
            project_id="film", spend=9.5, budget=10.0, request_id="monthly-2",
            budget_period_id="2026-10",
        )
        next_period = self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="monthly-3",
            budget_period_id="2026-11",
        )
        self.assertEqual(
            [item["event_id"] for item in first["alerts"]],
            [item["event_id"] for item in repeat["alerts"]],
        )
        self.assertNotEqual(
            [item["event_id"] for item in first["alerts"]],
            [item["event_id"] for item in next_period["alerts"]],
        )
        self.assertEqual(len(self.manager.data["events"]), 4)

    def test_legacy_period_omission_preserves_existing_idempotency(self):
        first = self.manager.record(
            project_id="film", spend=8.0, budget=10.0, request_id="job-1"
        )
        second = self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-2"
        )
        self.assertEqual(
            [item["event_id"] for item in first["alerts"]],
            [item["event_id"] for item in second["alerts"]],
        )
        self.assertEqual(len(self.manager.data["events"]), 2)

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

    def test_failed_delivery_uses_exponential_backoff_and_can_be_retried(self):
        manager = BudgetAlertManager(
            self.path, thresholds=(0.5,), clock=lambda: self.now[0],
            retry_base_seconds=30, retry_max_seconds=300,
        )
        manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-retry"
        )
        failed = manager.dispatch_pending(
            lambda alert: (_ for _ in ()).throw(RuntimeError("private webhook response")),
            limit=1,
        )
        self.assertEqual(failed["failed"], 1)
        pending = manager.pending_alerts(limit=1)["alerts"][0]
        self.assertEqual(pending["delivery_attempts"], 1)
        self.assertEqual(pending["last_delivery_error_code"], "DELIVERY_CALLBACK_FAILED")
        self.assertEqual(
            pending["next_delivery_at"],
            (self.now[0] + timedelta(seconds=30)).isoformat(),
        )
        self.assertNotIn("private webhook response", repr(pending))

        called = []
        deferred = manager.dispatch_pending(lambda alert: called.append(alert) or True, limit=1)
        self.assertEqual(deferred["attempted"], 0)
        self.assertEqual(deferred["deferred"], 1)
        self.assertEqual(called, [])

        self.now[0] += timedelta(seconds=30)
        retried = manager.dispatch_pending(lambda alert: {"delivered": True}, limit=10)
        self.assertEqual(retried["delivered"], 1)
        self.assertEqual(len(manager.pending_alerts()["alerts"]), 0)

    def test_delivery_attempts_exhaust_to_dead_letter_and_can_be_requeued(self):
        manager = BudgetAlertManager(
            self.path, clock=lambda: self.now[0], max_delivery_attempts=2,
            retry_base_seconds=10, retry_max_seconds=20,
        )
        event = manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-exhaust"
        )["alerts"][0]
        first = manager.dispatch_pending(lambda _: False, limit=1)
        self.assertEqual(first["results"][0]["status"], "DELIVERY_RETRY_SCHEDULED")
        self.now[0] += timedelta(seconds=10)
        second = manager.dispatch_pending(lambda _: False, limit=1)
        self.assertEqual(second["results"][0]["status"], "DELIVERY_EXHAUSTED")
        failed = manager.failed_alerts(limit=10)["alerts"]
        failed_event = next(item for item in failed if item["event_id"] == event["event_id"])
        self.assertEqual(failed_event["status"], "FAILED_PERMANENT")
        self.assertEqual(failed_event["delivery_attempts"], 2)
        self.assertNotIn(
            event["event_id"],
            [item["event_id"] for item in manager.pending_alerts(limit=10)["alerts"]],
        )
        requeued = manager.requeue_failed(event["event_id"])
        self.assertEqual(requeued["status"], "ALERT_REQUEUED")
        self.assertEqual(
            manager.dispatch_pending(lambda _: True, limit=1)["delivered"], 1
        )

    def test_concurrent_dispatchers_cannot_claim_the_same_events(self):
        self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-concurrent"
        )
        second_manager = BudgetAlertManager(self.path, clock=lambda: self.now[0])
        entered = threading.Event()
        release = threading.Event()
        seen = []

        def blocking_sender(alert):
            self.assertNotIn("delivery_lease_token", alert)
            self.assertNotIn("delivery_lease_owner", alert)
            self.assertNotIn("delivery_lease_until", alert)
            seen.append(alert["event_id"])
            if len(seen) == 1:
                entered.set()
                release.wait(timeout=3)
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                self.manager.dispatch_pending, blocking_sender, limit=2, worker_id="worker-one"
            )
            self.assertTrue(entered.wait(timeout=2))
            second = second_manager.dispatch_pending(
                lambda _: True, limit=2, worker_id="worker-two"
            )
            self.assertEqual(second["attempted"], 0)
            self.assertEqual(second["deferred"], 2)
            release.set()
            first = first_future.result(timeout=4)

        self.assertEqual(first["delivered"], 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(set(seen)), 2)

    def test_expired_dispatch_lease_is_recovered_after_worker_exit(self):
        event = self.manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-crashed-worker"
        )["alerts"][0]
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        stored["events"][event["event_id"]].update({
            "delivery_lease_token": "a" * 32,
            "delivery_lease_owner": "crashed-worker",
            "delivery_lease_until": (self.now[0] - timedelta(seconds=1)).isoformat(),
        })
        self.path.write_text(json.dumps(stored), encoding="utf-8")
        reloaded = BudgetAlertManager(self.path, clock=lambda: self.now[0])
        result = reloaded.dispatch_pending(lambda _: True, limit=1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["delivered"], 1)

    def test_dispatch_rejects_invalid_callback_limit_and_worker(self):
        self.assertEqual(self.manager.dispatch_pending(None)["status"], "INVALID_DELIVERY_CALLBACK")
        self.assertEqual(self.manager.dispatch_pending(lambda _: True, limit=0)["status"], "INVALID_LIMIT")
        self.assertEqual(
            self.manager.dispatch_pending(lambda _: True, worker_id="bad worker")["status"],
            "INVALID_WORKER_ID",
        )

    def test_invalid_inputs_and_period_id_fail_closed(self):
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
        invalid_period = self.manager.record(
            project_id="p", spend=8, budget=10, request_id="r", budget_period_id=" "
        )
        self.assertEqual(invalid_period["status"], "INVALID_BUDGET_PERIOD_ID")

    def test_pending_and_failed_limits_are_bounded(self):
        self.assertEqual(self.manager.pending_alerts(limit=0)["status"], "INVALID_LIMIT")
        self.assertEqual(self.manager.pending_alerts(limit=1001)["status"], "INVALID_LIMIT")
        self.assertEqual(self.manager.failed_alerts(limit=1001)["status"], "INVALID_LIMIT")

    def test_invalid_constructor_settings_are_rejected(self):
        for kwargs in (
            {"max_delivery_attempts": 0},
            {"max_delivery_attempts": True},
            {"retry_base_seconds": 0},
            {"retry_base_seconds": 60, "retry_max_seconds": 30},
            {"dispatch_lease_seconds": 4},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    BudgetAlertManager(self.path, **kwargs)

    def test_naive_clock_fails_closed(self):
        manager = BudgetAlertManager(
            self.path, clock=lambda: datetime(2026, 10, 16, 12)
        )
        result = manager.record(
            project_id="film", spend=9.0, budget=10.0, request_id="job-naive-clock"
        )
        self.assertEqual(result["status"], "INVALID_CLOCK")

    def test_corrupt_state_and_invalid_lease_are_rejected(self):
        self.path.write_text(json.dumps({"events": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now[0])

        self.path.write_text(json.dumps({"version": "1.0", "events": {
            "a" * 64: {
                "event_id": "a" * 64,
                "project_id": "film",
                "threshold": 0.5,
                "budget": 10.0,
                "spend": 8.0,
                "ratio": 0.8,
                "request_id": "job-1",
                "status": "PENDING",
                "created_at": self.now[0].isoformat(),
                "delivery_lease_token": "not-a-token",
                "delivery_lease_owner": "worker",
                "delivery_lease_until": (self.now[0] + timedelta(seconds=30)).isoformat(),
            }
        }}), encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(self.path, clock=lambda: self.now[0])


if __name__ == "__main__":
    unittest.main()
