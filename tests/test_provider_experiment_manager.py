"""Controlled A/B assignment and paid-usage safety tests."""
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_experiment_manager import ProviderABExperimentManager


class ProviderABExperimentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProviderABExperimentManager(Path(self.temp_dir.name) / "experiments.json")
        self.manager.configure(
            "router-experiment", control_provider_id="free-control",
            treatment_provider_id="paid-treatment", rollout_percent=100,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def select(self, *, approved=False, costs=None, zero=None):
        return self.manager.select(
            "router-experiment", project_id="film", request_id="job-1",
            candidate_provider_ids=["free-control", "paid-treatment"],
            candidate_costs=costs or {"free-control": 0.0, "paid-treatment": 0.25},
            zero_cost_provider_ids=zero if zero is not None else ["free-control"],
            paid_usage_approved=approved,
        )

    def test_paid_treatment_is_blocked_without_explicit_approval(self):
        result = self.select()
        self.assertEqual(result["status"], "PAID_EXPERIMENT_BLOCKED_CONTROL_SELECTED")
        self.assertEqual(result["selected_provider_id"], "free-control")

    def test_paid_treatment_requires_approval_and_stable_cohort(self):
        first = self.select(approved=True)
        second = self.select(approved=True)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "EXPERIMENT_TREATMENT_SELECTED")
        self.assertEqual(first["selected_provider_id"], "paid-treatment")

    def test_no_zero_cost_alternative_fails_closed(self):
        result = self.select(zero=[])
        self.assertEqual(result["status"], "PAID_EXPERIMENT_BLOCKED")
        self.assertIsNone(result["selected_provider_id"])

    def test_missing_candidate_and_invalid_cost_are_rejected(self):
        result = self.manager.select(
            "router-experiment", project_id="film", request_id="job-1",
            candidate_provider_ids=["free-control"],
            candidate_costs={"free-control": 0.0},
            zero_cost_provider_ids=["free-control"], paid_usage_approved=False,
        )
        self.assertEqual(result["status"], "EXPERIMENT_CANDIDATE_UNAVAILABLE")
        invalid = self.select(costs={"free-control": 0.0, "paid-treatment": float("nan")})
        self.assertEqual(invalid["status"], "EXPERIMENT_COST_UNVERIFIED")

    def test_disabled_experiment_uses_control(self):
        self.manager.configure(
            "router-experiment", control_provider_id="free-control",
            treatment_provider_id="paid-treatment", rollout_percent=100, enabled=False,
        )
        result = self.select(approved=True)
        self.assertEqual(result["status"], "EXPERIMENT_CONTROL_SELECTED")
        self.assertEqual(result["selected_provider_id"], "free-control")


if __name__ == "__main__":
    unittest.main()
