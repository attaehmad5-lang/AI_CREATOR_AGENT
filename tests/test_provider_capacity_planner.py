"""Deterministic capacity planning and synthetic provider load tests."""
import unittest

from projects.provider_store.provider_capacity_planner import ProviderCapacityPlanner


class ProviderCapacityPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = ProviderCapacityPlanner(max_requests=20)

    def test_plan_reports_concurrency_and_burst_queue_shortfalls(self):
        result = self.planner.plan(
            provider_id="image-provider",
            target_requests_per_second=20,
            p95_latency_ms=200,
            max_concurrent=4,
            max_queue_size=50,
            target_utilization=0.70,
            burst_window_seconds=5,
        )
        self.assertEqual(result["status"], "CAPACITY_PLAN_AVAILABLE")
        plan = result["plan"]
        self.assertEqual(plan["required_concurrency"], 6)
        self.assertEqual(plan["estimated_sustainable_requests_per_second"], 14.0)
        self.assertEqual(plan["required_queue_slots_for_burst"], 100)
        self.assertFalse(plan["capacity_sufficient"])
        self.assertEqual(plan["concurrency_shortfall"], 2)
        self.assertEqual(plan["queue_shortfall"], 50)

    def test_plan_accepts_sufficient_capacity_and_zero_queue(self):
        result = self.planner.plan(
            provider_id="voice-provider",
            target_requests_per_second=1,
            p95_latency_ms=100,
            max_concurrent=2,
            max_queue_size=5,
            burst_window_seconds=1,
        )
        self.assertTrue(result["plan"]["capacity_sufficient"])
        self.assertEqual(result["plan"]["required_concurrency"], 1)
        self.assertEqual(result["plan"]["required_queue_slots_for_burst"], 1)

    def test_plan_rejects_invalid_or_boolean_numeric_inputs(self):
        for value in (True, 0, float("nan"), float("inf"), "5"):
            result = self.planner.plan(
                provider_id="p", target_requests_per_second=value,
                p95_latency_ms=100, max_concurrent=1, max_queue_size=0,
            )
            self.assertEqual(result["status"], "INVALID_CAPACITY_INPUT")

    def test_plan_rejects_invalid_identifiers_and_limits(self):
        self.assertEqual(
            self.planner.plan(
                provider_id=" ", target_requests_per_second=1,
                p95_latency_ms=100, max_concurrent=1, max_queue_size=0,
            )["status"],
            "INVALID_PROVIDER_ID",
        )
        self.assertEqual(
            self.planner.plan(
                provider_id="p", target_requests_per_second=1,
                p95_latency_ms=100, max_concurrent=True, max_queue_size=0,
            )["status"],
            "INVALID_CAPACITY_INPUT",
        )

    def test_load_simulation_reports_queueing_and_rejections_without_provider_calls(self):
        arrivals = [
            {"arrival_ms": 0, "service_ms": 100},
            {"arrival_ms": 0, "service_ms": 100},
            {"arrival_ms": 0, "service_ms": 100},
            {"arrival_ms": 0, "service_ms": 100},
            {"arrival_ms": 0, "service_ms": 100},
        ]
        result = self.planner.simulate_load(
            provider_id="image-provider", arrivals=arrivals,
            max_concurrent=2, max_queue_size=2,
        )
        simulation = result["simulation"]
        self.assertEqual(result["status"], "LOAD_SIMULATION_COMPLETED")
        self.assertEqual(simulation["offered_requests"], 5)
        self.assertEqual(simulation["accepted_requests"], 4)
        self.assertEqual(simulation["rejected_requests"], 1)
        self.assertEqual(simulation["max_queue_depth"], 2)
        self.assertEqual(simulation["p95_queue_wait_ms"], 100)
        self.assertEqual(simulation["simulated_drain_time_ms"], 200)
        self.assertEqual(simulation["provider_calls_made"], 0)
        self.assertFalse(simulation["paid_usage_incurred"])

    def test_load_simulation_starts_new_work_when_a_worker_becomes_free(self):
        arrivals = [
            {"arrival_ms": 0, "service_ms": 50},
            {"arrival_ms": 20, "service_ms": 30},
            {"arrival_ms": 50, "service_ms": 10},
        ]
        result = self.planner.simulate_load(
            provider_id="p", arrivals=arrivals,
            max_concurrent=1, max_queue_size=0,
        )
        simulation = result["simulation"]
        self.assertEqual(simulation["accepted_requests"], 3)
        self.assertEqual(simulation["rejected_requests"], 0)
        self.assertEqual(simulation["simulated_drain_time_ms"], 60)
        self.assertEqual(simulation["queue_wait_sample_count"], 0)

    def test_load_simulation_fifo_waits_are_measured(self):
        arrivals = [
            {"arrival_ms": 0, "service_ms": 10},
            {"arrival_ms": 1, "service_ms": 10},
            {"arrival_ms": 2, "service_ms": 10},
        ]
        result = self.planner.simulate_load(
            provider_id="p", arrivals=arrivals,
            max_concurrent=1, max_queue_size=2,
        )
        simulation = result["simulation"]
        self.assertEqual(simulation["accepted_requests"], 3)
        self.assertEqual(simulation["p95_queue_wait_ms"], 18)
        self.assertEqual(simulation["simulated_drain_time_ms"], 30)

    def test_load_simulation_fails_closed_on_unsorted_or_malformed_profile(self):
        unsorted = [
            {"arrival_ms": 2, "service_ms": 1},
            {"arrival_ms": 1, "service_ms": 1},
        ]
        malformed = [{"arrival_ms": 0, "service_ms": 1, "payload": "must-not-be-accepted"}]
        for arrivals in (unsorted, malformed):
            self.assertEqual(
                self.planner.simulate_load(
                    provider_id="p", arrivals=arrivals,
                    max_concurrent=1, max_queue_size=1,
                )["status"],
                "INVALID_LOAD_PROFILE",
            )

    def test_load_simulation_enforces_input_count_and_numeric_bounds(self):
        too_many = [{"arrival_ms": i, "service_ms": 1} for i in range(21)]
        self.assertEqual(
            self.planner.simulate_load(
                provider_id="p", arrivals=too_many,
                max_concurrent=1, max_queue_size=1,
            )["status"],
            "INVALID_LOAD_PROFILE",
        )
        invalid = [{"arrival_ms": True, "service_ms": 1}]
        self.assertEqual(
            self.planner.simulate_load(
                provider_id="p", arrivals=invalid,
                max_concurrent=1, max_queue_size=1,
            )["status"],
            "INVALID_LOAD_PROFILE",
        )

    def test_empty_load_profile_is_safe(self):
        result = self.planner.simulate_load(
            provider_id="p", arrivals=[], max_concurrent=1, max_queue_size=0,
        )
        self.assertEqual(result["simulation"]["offered_requests"], 0)
        self.assertEqual(result["simulation"]["estimated_worker_utilization"], 0.0)


if __name__ == "__main__":
    unittest.main()
