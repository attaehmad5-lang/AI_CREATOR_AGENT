import unittest

from projects.provider_store.provider_ab_benchmark_manager import ProviderABBenchmarkManager


class ProviderABBenchmarkManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = ProviderABBenchmarkManager()
        self.candidates = [
            {"provider_id": "provider-a", "model_id": "model-1", "estimated_cost_per_run": 0},
            {"provider_id": "provider-b", "model_id": "model-2", "estimated_cost_per_run": 0},
        ]

    def test_free_benchmark_plan_is_side_effect_free(self):
        result = self.manager.plan(
            benchmark_id="compare-1", category="image", candidates=self.candidates,
            repetitions=3, max_total_cost=0,
        )
        self.assertEqual(result["status"], "BENCHMARK_PLAN_READY")
        self.assertFalse(result["executable"])
        self.assertFalse(result["execution_started"])
        self.assertEqual(result["provider_calls_made"], 0)
        self.assertEqual(result["estimated_total_cost"], 0)

    def test_paid_plan_requires_matching_explicit_approval_and_budget(self):
        candidates = [
            {"provider_id": "provider-a", "model_id": "model-1", "estimated_cost_per_run": 0.1},
            {"provider_id": "provider-b", "model_id": "model-2", "estimated_cost_per_run": 0.2},
        ]
        base = dict(
            benchmark_id="paid-compare", category="image", candidates=candidates,
            repetitions=2, max_total_cost=1.0,
        )
        self.assertEqual(
            self.manager.plan(**base)["status"],
            "PAID_BENCHMARK_APPROVAL_REQUIRED",
        )
        self.assertEqual(
            self.manager.plan(**base, allow_paid=True, paid_approval={
                "approved": True, "benchmark_id": "other", "approved_by": "admin",
            })["status"],
            "PAID_BENCHMARK_APPROVAL_REQUIRED",
        )
        result = self.manager.plan(**base, allow_paid=True, paid_approval={
            "approved": True, "benchmark_id": "paid-compare", "approved_by": "admin",
        })
        self.assertEqual(result["status"], "BENCHMARK_PLAN_READY")
        self.assertEqual(result["estimated_total_cost"], 0.6)
        self.assertTrue(result["paid_usage_approved"])
        self.assertEqual(result["provider_calls_made"], 0)

    def test_budget_overrun_is_blocked_even_with_approval(self):
        candidates = [
            {"provider_id": "provider-a", "model_id": "model-1", "estimated_cost_per_run": 1.0},
            {"provider_id": "provider-b", "model_id": "model-2", "estimated_cost_per_run": 1.0},
        ]
        result = self.manager.plan(
            benchmark_id="budget-1", category="image", candidates=candidates,
            repetitions=2, max_total_cost=1.0, allow_paid=True,
            paid_approval={"approved": True, "benchmark_id": "budget-1", "approved_by": "admin"},
        )
        self.assertEqual(result["status"], "BENCHMARK_BUDGET_EXCEEDED")
        self.assertFalse(result["executable"])

    def test_duplicate_and_malformed_candidates_fail_closed(self):
        duplicate = [self.candidates[0], dict(self.candidates[0])]
        self.assertEqual(
            self.manager.plan(benchmark_id="dup", category="image", candidates=duplicate)["status"],
            "DUPLICATE_CANDIDATE",
        )
        self.assertEqual(
            self.manager.plan(benchmark_id="bad", category="image", candidates=[{}, {}])["status"],
            "INVALID_CANDIDATE",
        )

    def test_invalid_limits_and_candidate_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderABBenchmarkManager(max_repetitions=True)
        self.assertEqual(
            self.manager.plan(benchmark_id="few", category="image", candidates=[self.candidates[0]])["status"],
            "INVALID_CANDIDATE_COUNT",
        )
        self.assertEqual(
            self.manager.plan(benchmark_id="runs", category="image", candidates=self.candidates, repetitions=101)["status"],
            "INVALID_REPETITIONS",
        )


if __name__ == "__main__":
    unittest.main()
