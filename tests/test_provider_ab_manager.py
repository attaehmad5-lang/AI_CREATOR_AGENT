"""Controlled A/B experiment policy and spend-safety tests."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_ab_manager import ProviderABExperimentManager


class ProviderABExperimentManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "experiments.json"
        self.manager = ProviderABExperimentManager(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def arms(self, cost_a=0.0, cost_b=0.0):
        return [
            {"arm_id": "A", "provider_id": "provider-a", "model_id": "model-a", "estimated_cost": cost_a},
            {"arm_id": "B", "provider_id": "provider-b", "model_id": "model-b", "estimated_cost": cost_b},
        ]

    def create(self, **kwargs):
        args = {
            "experiment_id": "exp-1", "project_id": "project-1",
            "modality": "image", "arms": self.arms(),
        }
        args.update(kwargs)
        return self.manager.create_experiment(**args)

    def test_free_only_experiment_assignments_are_deterministic_and_idempotent(self):
        self.assertTrue(self.create()["saved"])
        first = self.manager.assign(experiment_id="exp-1", project_id="project-1", request_id="req-1")
        duplicate = self.manager.assign(experiment_id="exp-1", project_id="project-1", request_id="req-1")
        self.assertEqual(first["arm_id"], duplicate["arm_id"])
        self.assertEqual(duplicate["status"], "ALREADY_ASSIGNED")
        reload_manager = ProviderABExperimentManager(self.path)
        again = reload_manager.assign(experiment_id="exp-1", project_id="project-1", request_id="req-1")
        self.assertEqual(first["arm_id"], again["arm_id"])
        self.assertEqual(again["status"], "ALREADY_ASSIGNED")

    def test_paid_experiment_creation_requires_explicit_approval_and_budget_caps(self):
        result = self.create(arms=self.arms(0.1, 0.2))
        self.assertEqual(result["status"], "PAID_AB_REQUIRES_APPROVAL_AND_BUDGET_CAPS")
        approved = self.create(
            arms=self.arms(0.1, 0.2), paid_usage_approved=True,
            approval_reference="approval-ticket-123",
            experiment_budget_cap=2.0, project_budget_cap=5.0,
        )
        self.assertTrue(approved["saved"])

    def test_paid_assignment_is_blocked_without_budget_reserver(self):
        self.create(
            arms=self.arms(0.1, 0.2), paid_usage_approved=True,
            approval_reference="approval-ticket-123",
            experiment_budget_cap=2.0, project_budget_cap=5.0,
        )
        results = [
            self.manager.assign(experiment_id="exp-1", project_id="project-1", request_id=f"req-{i}")
            for i in range(20)
        ]
        self.assertTrue(all(not r["assigned"] for r in results))
        self.assertTrue(all(r["status"] == "PAID_ARM_BLOCKED_NO_RESERVATION" for r in results))

    def test_paid_assignment_calls_idempotent_reserver_before_returning_arm(self):
        self.create(
            arms=self.arms(0.1, 0.2), paid_usage_approved=True,
            approval_reference="approval-ticket-123",
            experiment_budget_cap=2.0, project_budget_cap=5.0,
        )
        reservations = []
        def reserve(**kwargs):
            reservations.append(kwargs)
            return {"saved": True, "status": "RESERVED"}
        result = self.manager.assign(
            experiment_id="exp-1", project_id="project-1", request_id="req-paid",
            reserve_paid_usage=reserve,
        )
        self.assertTrue(result["assigned"])
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["approval_reference"], "approval-ticket-123")
        self.assertEqual(reservations[0]["request_id"], "ab:exp-1:req-paid")
        duplicate = self.manager.assign(
            experiment_id="exp-1", project_id="project-1", request_id="req-paid",
            reserve_paid_usage=reserve,
        )
        self.assertEqual(duplicate["status"], "ALREADY_ASSIGNED")
        self.assertEqual(len(reservations), 1)

    def test_paid_reservation_failure_fails_closed(self):
        self.create(
            arms=self.arms(0.1, 0.2), paid_usage_approved=True,
            approval_reference="approval-ticket-123",
            experiment_budget_cap=2.0, project_budget_cap=5.0,
        )
        result = self.manager.assign(
            experiment_id="exp-1", project_id="project-1", request_id="req-paid",
            reserve_paid_usage=lambda **kwargs: {"saved": False},
        )
        self.assertEqual(result["status"], "PAID_BUDGET_RESERVATION_FAILED")
        self.assertFalse(result["assigned"])

    def test_experiment_budget_cap_stops_new_paid_assignments(self):
        self.create(
            arms=[{"arm_id":"A","provider_id":"provider-a","model_id":"model-a","estimated_cost":0.5},
                  {"arm_id":"B","provider_id":"provider-b","model_id":"model-b","estimated_cost":0.5}],
            paid_usage_approved=True, approval_reference="approved-1",
            experiment_budget_cap=0.5, project_budget_cap=3.0,
        )
        reserve = lambda **kwargs: {"saved": True}
        assigned = self.manager.assign(
            experiment_id="exp-1", project_id="project-1", request_id="req-1",
            reserve_paid_usage=reserve,
        )
        self.assertTrue(assigned["assigned"])
        next_result = self.manager.assign(
            experiment_id="exp-1", project_id="project-1", request_id="req-2",
            reserve_paid_usage=reserve,
        )
        self.assertEqual(next_result["status"], "EXPERIMENT_BUDGET_EXHAUSTED")

    def test_project_scope_and_invalid_arms_fail_closed(self):
        self.assertEqual(self.create(arms=[self.arms()[0]])["status"], "INVALID_EXPERIMENT_ARMS")
        self.create()
        result = self.manager.assign(experiment_id="exp-1", project_id="other-project", request_id="req")
        self.assertEqual(result["status"], "PROJECT_SCOPE_MISMATCH")

    def test_outcome_is_idempotent_and_summary_contains_only_aggregate_metrics(self):
        self.create()
        assignment = self.manager.assign(experiment_id="exp-1", project_id="project-1", request_id="req-1")
        self.assertTrue(assignment["assigned"])
        args = {
            "experiment_id": "exp-1", "project_id": "project-1", "request_id": "req-1",
            "success": True, "latency_ms": 123.0, "actual_cost": 0.0, "quality_score": 0.9,
        }
        self.assertEqual(self.manager.record_outcome(**args)["status"], "OUTCOME_RECORDED")
        self.assertEqual(self.manager.record_outcome(**args)["status"], "ALREADY_RECORDED")
        conflict = dict(args, success=False)
        self.assertEqual(self.manager.record_outcome(**conflict)["status"], "OUTCOME_CONFLICT")
        summary = self.manager.summary(experiment_id="exp-1", project_id="project-1")
        self.assertEqual(summary["status"], "AB_SUMMARY_AVAILABLE")
        self.assertEqual(sum(arm["outcome_count"] for arm in summary["results"]), 1)
        self.assertNotIn("request_id", repr(summary))

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderABExperimentManager(self.path)


if __name__ == "__main__":
    unittest.main()
