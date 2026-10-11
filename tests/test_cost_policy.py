import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.cost_policy import ProviderCostPolicy


def _reserve_cost_in_process(args):
    ledger_path, request_id = args
    policy = ProviderCostPolicy(
        ledger_path, project_budgets={"documentary": 5.0}
    )
    return policy.reserve_usage(
        project_id="documentary",
        provider_id="paid",
        estimated_cost=2.0,
        request_id=request_id,
    )["status"]


class ProviderCostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp_dir.name) / "usage_ledger.json"
        self.policy = ProviderCostPolicy(self.ledger, project_budgets={"documentary": 5.0})

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unverified_numeric_credits_cannot_authorize_free_usage(self):
        provider = {"id": "free", "enabled": True, "subscription_required": False}
        result = self.policy.evaluate_request(
            provider, project_id="documentary", estimated_cost=2.0,
            free_credits_remaining=3.0, paid_usage_approved=False,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "VERIFIED_CREDIT_MANAGER_REQUIRED")

    def test_paid_usage_is_blocked_without_explicit_approval(self):
        result = self.policy.evaluate_request(
            {"id": "paid", "enabled": True, "subscription_required": True},
            project_id="documentary", estimated_cost=1.0,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PAID_APPROVAL_REQUIRED")

    def test_paid_usage_requires_budget_and_approval(self):
        result = self.policy.evaluate_request(
            {"id": "paid", "enabled": True}, project_id="documentary",
            estimated_cost=1.0, paid_usage_approved=True,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["billing_mode"], "PAID_APPROVED")

    def test_paid_usage_requires_a_configured_budget(self):
        policy = ProviderCostPolicy()
        result = policy.evaluate_request(
            {"id": "paid", "enabled": True},
            project_id="unbudgeted",
            estimated_cost=0.5,
            paid_usage_approved=True,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PROJECT_BUDGET_REQUIRED")

    def test_paid_approval_and_provider_enabled_flags_must_be_boolean(self):
        invalid_approval = self.policy.evaluate_request(
            {"id": "paid", "enabled": True},
            project_id="documentary",
            estimated_cost=0.5,
            paid_usage_approved="yes",
        )
        self.assertEqual(invalid_approval["status"], "INVALID_PAID_APPROVAL_FLAG")
        invalid_enabled = self.policy.evaluate_request(
            {"id": "paid", "enabled": "false"},
            project_id="documentary",
            estimated_cost=0.5,
            paid_usage_approved=True,
        )
        self.assertEqual(invalid_enabled["status"], "INVALID_PROVIDER_CONFIGURATION")

    def test_project_budget_is_hard_stop(self):
        result = self.policy.evaluate_request(
            {"id": "paid", "enabled": True}, project_id="documentary",
            estimated_cost=6.0, paid_usage_approved=True,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PROJECT_BUDGET_EXCEEDED")

    def test_provider_selection_rejects_caller_supplied_free_credit_balance(self):
        result = self.policy.select_provider(
            [{"id": "paid", "enabled": True}, {"id": "free", "enabled": True}],
            project_id="documentary",
            estimated_costs={"paid": 1.0, "free": 1.0},
            free_credits={"free": 2.0},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "VERIFIED_CREDIT_MANAGER_REQUIRED")

    def test_provider_selection_rejects_malformed_credit_amount_without_crashing(self):
        result = self.policy.select_provider(
            [{"id": "free", "enabled": True}],
            project_id="documentary",
            estimated_costs={"free": 1.0},
            free_credits={"free": "not-a-number"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_FREE_CREDIT_MAP")

    def test_provider_selection_rejects_invalid_credit_map_type(self):
        result = self.policy.select_provider(
            [{"id": "free", "enabled": True}],
            project_id="documentary",
            estimated_costs={"free": 1.0},
            free_credits=["invalid"],
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_FREE_CREDIT_MAP")

    def test_provider_selection_rejects_invalid_selection_input(self):
        result = self.policy.select_provider(
            "not-a-provider-list",
            project_id="documentary",
            estimated_costs={"free": 1.0},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_SELECTION_INPUT")

    def test_reconciliation_is_idempotent_and_persists_actual_usage(self):
        first = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid", estimated_cost=1.0,
            actual_cost=1.25, request_id="request-1",
        )
        duplicate = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid", estimated_cost=1.0,
            actual_cost=1.25, request_id="request-1",
        )
        self.assertEqual(first["status"], "RECONCILED")
        self.assertAlmostEqual(first["variance"], 0.25)
        self.assertEqual(duplicate["status"], "DUPLICATE_REQUEST")
        reloaded = ProviderCostPolicy(self.ledger, project_budgets={"documentary": 5.0})
        self.assertAlmostEqual(reloaded.ledger["projects"]["documentary"]["actual_spend"], 1.25)

    def test_budget_reservations_prevent_overcommit_and_are_idempotent(self):
        first = self.policy.reserve_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=3.0, request_id="job-1",
        )
        duplicate = self.policy.reserve_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=3.0, request_id="job-1",
        )
        over_budget = self.policy.reserve_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=3.0, request_id="job-2",
        )
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(duplicate["status"], "ALREADY_RESERVED")
        self.assertEqual(over_budget["status"], "PROJECT_BUDGET_EXCEEDED")
        decision = self.policy.evaluate_request(
            {"id": "paid", "enabled": True}, project_id="documentary",
            estimated_cost=3.0, paid_usage_approved=True,
        )
        self.assertEqual(decision["status"], "PROJECT_BUDGET_EXCEEDED")

    def test_concurrent_reservations_cannot_overcommit_project_budget(self):
        def reserve(index):
            return self.policy.reserve_usage(
                project_id="documentary",
                provider_id="paid",
                estimated_cost=2.0,
                request_id=f"concurrent-{index}",
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(result["status"] == "RESERVED" for result in results), 2)
        self.assertEqual(sum(result["status"] == "PROJECT_BUDGET_EXCEEDED" for result in results), 6)

    def test_reconciliation_consumes_reservation_and_release_frees_budget(self):
        self.policy.reserve_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=3.0, request_id="job-1",
        )
        reconciled = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=3.0, actual_cost=2.5, request_id="job-1",
        )
        self.assertEqual(reconciled["status"], "RECONCILED")
        self.assertNotIn("job-1", self.policy.ledger["projects"]["documentary"]["reservations"])
        self.policy.reserve_usage(
            project_id="documentary", provider_id="paid",
            estimated_cost=2.0, request_id="job-2",
        )
        retained = self.policy.release_reservation(project_id="documentary", request_id="job-2")
        self.assertEqual(retained["status"], "RESERVATION_RETAINED_CHARGE_OUTCOME_UNKNOWN")
        self.assertIn("job-2", self.policy.ledger["projects"]["documentary"]["reservations"])
        requires_reconciliation = self.policy.release_reservation(
            project_id="documentary", request_id="job-2", charge_outcome="incurred"
        )
        self.assertEqual(requires_reconciliation["status"], "RECONCILIATION_REQUIRED")
        self.assertIn("job-2", self.policy.ledger["projects"]["documentary"]["reservations"])
        released = self.policy.release_reservation(
            project_id="documentary", request_id="job-2", charge_outcome="not_incurred"
        )
        self.assertEqual(released["status"], "RESERVATION_RELEASED")
        self.assertEqual(
            self.policy.release_reservation(
                project_id="documentary", request_id="job-2", charge_outcome="not_incurred"
            )["status"],
            "RESERVATION_NOT_FOUND",
        )

    def test_reconciliation_rejects_reservation_provider_or_estimate_mismatch(self):
        self.policy.reserve_usage(
            project_id="documentary", provider_id="paid-a",
            estimated_cost=2.0, request_id="job-conflict",
        )
        wrong_provider = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid-b",
            estimated_cost=2.0, actual_cost=1.0, request_id="job-conflict",
        )
        self.assertEqual(wrong_provider["status"], "REQUEST_ID_CONFLICT")
        wrong_estimate = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid-a",
            estimated_cost=3.0, actual_cost=1.0, request_id="job-conflict",
        )
        self.assertEqual(wrong_estimate["status"], "REQUEST_ID_CONFLICT")
        project = self.policy.ledger["projects"]["documentary"]
        self.assertIn("job-conflict", project["reservations"])
        self.assertNotIn("job-conflict", project["requests"])

    def test_malformed_charge_outcomes_fail_closed_without_exception(self):
        for value in ([], {}, 1, None):
            with self.subTest(value=value):
                result = self.policy.release_reservation(
                    project_id="documentary",
                    request_id="invalid-charge-outcome",
                    charge_outcome=value,
                )
                self.assertEqual(result["status"], "INVALID_CHARGE_OUTCOME")
                self.assertFalse(result["saved"])

    def test_unknown_charge_outcome_does_not_free_quota_for_another_request(self):
        self.policy.reserve_usage(
            project_id="documentary", provider_id="paid", estimated_cost=4.0, request_id="timeout-job"
        )
        outcome = self.policy.release_reservation(
            project_id="documentary", request_id="timeout-job", charge_outcome="unknown"
        )
        self.assertEqual(outcome["status"], "RESERVATION_RETAINED_CHARGE_OUTCOME_UNKNOWN")
        blocked = self.policy.reserve_usage(
            project_id="documentary", provider_id="paid", estimated_cost=5.0, request_id="next-job"
        )
        self.assertEqual(blocked["status"], "PROJECT_BUDGET_EXCEEDED")

    def test_concurrent_budget_reservations_across_processes_never_overspend(self):
        ledger_path = self.ledger.with_name("process-ledger.json")
        args = [
            (str(ledger_path), f"process-paid-job-{index}")
            for index in range(20)
        ]
        with ProcessPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_reserve_cost_in_process, args))

        self.assertEqual(statuses.count("RESERVED"), 2)
        self.assertEqual(statuses.count("PROJECT_BUDGET_EXCEEDED"), 18)
        loaded = ProviderCostPolicy(
            ledger_path, project_budgets={"documentary": 5.0}
        )
        reserved = loaded.ledger["projects"]["documentary"]["reservations"]
        self.assertEqual(sum(item["estimated_cost"] for item in reserved.values()), 4.0)

    def test_actual_cost_overrun_triggers_budget_alert(self):
        result = self.policy.reconcile_usage(
            project_id="documentary", provider_id="paid", estimated_cost=4.0,
            actual_cost=5.5, request_id="request-2",
        )
        self.assertEqual(result["status"], "BUDGET_EXCEEDED")
        self.assertTrue(result["budget_exceeded"])

    def test_invalid_cost_values_are_rejected(self):
        result = self.policy.evaluate_request(
            {"id": "free"}, project_id="documentary", estimated_cost=float("nan")
        )
        self.assertEqual(result["status"], "INVALID_COST_AMOUNT")

    def test_invalid_project_budgets_are_rejected_at_construction(self):
        for budgets in ({"documentary": float("nan")}, {"documentary": float("inf")}, {"": 1.0}, []):
            with self.subTest(budgets=budgets):
                with self.assertRaises(ValueError):
                    ProviderCostPolicy(project_budgets=budgets)

    def test_corrupt_usage_ledger_records_are_rejected(self):
        corrupt_ledgers = (
            {"projects": {"documentary": {"actual_spend": "unknown", "requests": {}}}},
            {"projects": {"documentary": {"actual_spend": float("nan"), "requests": {}}}},
            {"projects": {"documentary": {"actual_spend": 0.0, "requests": []}}},
            {"projects": {"": {"actual_spend": 0.0, "requests": {}}}},
        )
        for index, ledger in enumerate(corrupt_ledgers):
            with self.subTest(index=index):
                path = self.ledger.with_name(f"corrupt-{index}.json")
                path.write_text(json.dumps(ledger), encoding="utf-8")
                with self.assertRaises(ValueError):
                    ProviderCostPolicy(path)


if __name__ == "__main__":
    unittest.main()
