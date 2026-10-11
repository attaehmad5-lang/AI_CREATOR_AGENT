import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.cost_policy import ProviderCostPolicy


class ProviderCostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ledger = Path(self.temp_dir.name) / "usage_ledger.json"
        self.policy = ProviderCostPolicy(self.ledger, project_budgets={"documentary": 5.0})

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_free_credits_are_used_before_paid_approval(self):
        provider = {"id": "free", "enabled": True, "subscription_required": False}
        result = self.policy.evaluate_request(
            provider, project_id="documentary", estimated_cost=2.0,
            free_credits_remaining=3.0, paid_usage_approved=False,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["billing_mode"], "FREE_CREDITS")

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

    def test_project_budget_is_hard_stop(self):
        result = self.policy.evaluate_request(
            {"id": "paid", "enabled": True}, project_id="documentary",
            estimated_cost=6.0, paid_usage_approved=True,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PROJECT_BUDGET_EXCEEDED")

    def test_provider_selection_prefers_free_eligible_provider(self):
        result = self.policy.select_provider(
            [{"id": "paid", "enabled": True}, {"id": "free", "enabled": True}],
            project_id="documentary",
            estimated_costs={"paid": 1.0, "free": 1.0},
            free_credits={"free": 2.0},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "free")
        self.assertEqual(result["billing_mode"], "FREE_CREDITS")

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


if __name__ == "__main__":
    unittest.main()
