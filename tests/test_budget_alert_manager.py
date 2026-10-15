import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.budget_alert_manager import BudgetAlertManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.provider_service import ProviderService


class _SnapshotPolicy:
    def __init__(self, *, budget=100.0, spent=0.0, reserved=0.0, status="OK"):
        self.snapshot = {
            "status": status, "project_id": "project",
            "budget": budget, "spent": spent, "reserved": reserved,
            "remaining": max(0.0, budget - spent - reserved),
        }

    def budget_snapshot(self, project_id):
        return {**self.snapshot, "project_id": project_id}


class _Connection:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class BudgetAlertManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_50_case_usage_matrix_emits_each_threshold_once(self):
        # Fifty usage levels (2%..100%) check every threshold-crossing combination.
        for index in range(1, 51):
            with self.subTest(usage_pct=index * 2):
                policy = _SnapshotPolicy(spent=float(index * 2))
                path = self.root / f"matrix-{index}.json"
                delivered = []
                manager = BudgetAlertManager(policy, path, notifier=delivered.append)
                result = manager.evaluate_project(f"project-{index}")
                expected = [x for x in (50, 75, 90, 100) if x <= index * 2]
                self.assertEqual(
                    [event["threshold_pct"] for event in result["alerts"]],
                    expected,
                )
                self.assertEqual(result["new_alert_count"], len(expected))
                self.assertEqual(result["delivered_count"], len(expected))
                self.assertEqual(len(delivered), len(expected))
                repeated = manager.evaluate_project(f"project-{index}")
                self.assertEqual(repeated["new_alert_count"], 0)
                self.assertEqual(repeated["delivered_count"], 0)
                self.assertEqual(len(manager.list_events(f"project-{index}")["events"]), len(expected))

    def test_reserved_cost_counts_toward_budget_threshold(self):
        policy = _SnapshotPolicy(spent=30.0, reserved=20.0)
        manager = BudgetAlertManager(policy, self.root / "reserved.json")
        result = manager.evaluate_project("film")
        self.assertEqual(result["usage_pct"], 50.0)
        self.assertEqual([e["threshold_pct"] for e in result["alerts"]], [50])

    def test_delivery_failure_is_retained_and_retried_with_stable_event_id(self):
        attempts = []
        def flaky(event):
            attempts.append(event["event_id"])
            if len(attempts) == 1:
                raise OSError("private delivery detail")
        policy = _SnapshotPolicy(spent=50.0)
        path = self.root / "retry.json"
        manager = BudgetAlertManager(policy, path, notifier=flaky)
        first = manager.evaluate_project("film")
        self.assertEqual(first["alerts"][0]["delivery_status"], "PENDING")
        self.assertNotIn("private delivery detail", repr(first))
        second = manager.evaluate_project("film")
        self.assertEqual(second["delivered_count"], 1)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(
            {e["delivery_status"] for e in second["alerts"]},
            {"DELIVERED"},
        )

    def test_missing_notifier_keeps_events_for_later_delivery(self):
        policy = _SnapshotPolicy(spent=100.0)
        path = self.root / "later.json"
        first = BudgetAlertManager(policy, path).evaluate_project("film")
        self.assertEqual(first["new_alert_count"], 4)
        self.assertTrue(all(e["delivery_status"] == "NOT_CONFIGURED" for e in first["alerts"]))
        delivered = []
        second = BudgetAlertManager(policy, path, notifier=delivered.append).evaluate_project("film")
        self.assertEqual(second["delivered_count"], 4)
        self.assertEqual(len(delivered), 4)

    def test_budget_change_creates_a_new_threshold_event_set(self):
        policy = _SnapshotPolicy(budget=100.0, spent=75.0)
        manager = BudgetAlertManager(policy, self.root / "budget-change.json")
        first = manager.evaluate_project("film")
        self.assertEqual(first["new_alert_count"], 2)
        policy.snapshot["budget"] = 200.0
        policy.snapshot["spent"] = 150.0
        second = manager.evaluate_project("film")
        self.assertEqual(second["new_alert_count"], 2)
        self.assertEqual(len(manager.list_events("film")["events"]), 4)

    def test_budget_not_configured_and_invalid_projects_fail_closed(self):
        policy = _SnapshotPolicy(status="PROJECT_BUDGET_REQUIRED")
        manager = BudgetAlertManager(policy, self.root / "missing-budget.json")
        self.assertEqual(manager.evaluate_project("film")["status"], "PROJECT_BUDGET_REQUIRED")
        self.assertEqual(manager.evaluate_project("")["status"], "INVALID_PROJECT")
        self.assertEqual(manager.list_events("")["status"], "INVALID_PROJECT")
        self.assertEqual(manager.list_events("film")["events"], [])

    def test_invalid_thresholds_and_notifier_are_rejected(self):
        policy = _SnapshotPolicy()
        for thresholds in ((), (75, 50), (50, 50), (0, 50), (101,), (True,)):
            with self.subTest(thresholds=thresholds):
                with self.assertRaises(ValueError):
                    BudgetAlertManager(policy, self.root / "invalid.json", thresholds=thresholds)
        with self.assertRaises(ValueError):
            BudgetAlertManager(policy, self.root / "invalid-notifier.json", notifier=object())

    def test_corrupt_alert_ledger_fails_closed(self):
        path = self.root / "corrupt.json"
        path.write_text("{bad json", encoding="utf-8")
        with self.assertRaises(ValueError):
            BudgetAlertManager(_SnapshotPolicy(), path)

    def test_usage_finalization_triggers_budget_alerts(self):
        policy = ProviderCostPolicy(
            self.root / "cost-ledger.json",
            project_budgets={"film": 100.0},
        )
        delivered = []
        alerts = BudgetAlertManager(
            policy, self.root / "service-alerts.json", notifier=delivered.append
        )
        service = ProviderService(
            _Connection(), cost_policy=policy, budget_alert_manager=alerts
        )
        result = service.finalize_started_request(
            project_id="film",
            provider_id="image",
            request_id="budget-alert-integration",
            estimated_cost=60.0,
            actual_cost=60.0,
            success=True,
            charge_outcome="incurred",
        )
        self.assertTrue(result["finalized"])
        self.assertEqual(result["budget_alerts"]["new_alert_count"], 1)
        self.assertEqual(
            [event["threshold_pct"] for event in delivered],
            [50],
        )


if __name__ == "__main__":
    unittest.main()
