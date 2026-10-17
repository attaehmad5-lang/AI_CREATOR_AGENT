import unittest

from projects.provider_store.provider_capacity_planner import ProviderCapacityPlanner


class ProviderCapacityPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = ProviderCapacityPlanner()

    def test_capacity_estimate_uses_p95_latency_utilization_and_burst(self):
        result = self.planner.estimate(
            target_requests_per_second=20,
            p95_latency_ms=500,
            target_utilization=0.5,
            burst_factor=2,
            max_workers=100,
        )
        self.assertEqual(result["status"], "CAPACITY_ESTIMATE")
        self.assertEqual(result["expected_in_flight_requests"], 10.0)
        self.assertEqual(result["recommended_workers"], 40)
        self.assertTrue(result["within_configured_worker_limit"])
        self.assertTrue(result["production_load_test_required"])

    def test_capacity_estimate_warns_when_worker_limit_is_too_low(self):
        result = self.planner.estimate(
            target_requests_per_second=100,
            p95_latency_ms=1000,
            max_workers=10,
        )
        self.assertGreater(result["recommended_workers"], 10)
        self.assertFalse(result["within_configured_worker_limit"])

    def test_sample_summary_computes_rates_and_nearest_rank_p95(self):
        samples = [
            {"success": True, "latency_ms": latency}
            for latency in range(1, 21)
        ]
        samples[0]["success"] = False
        samples[1]["timed_out"] = True
        result = self.planner.summarize_samples(samples, window_seconds=10)
        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["observed_requests_per_second"], 2.0)
        self.assertEqual(result["success_rate"], 0.95)
        self.assertEqual(result["timeout_rate"], 0.05)
        self.assertEqual(result["p95_latency_ms"], 19.0)
        self.assertTrue(result["production_load_test_required"])

    def test_invalid_estimate_values_fail_closed(self):
        cases = [
            {"target_requests_per_second": 0, "p95_latency_ms": 100},
            {"target_requests_per_second": float("nan"), "p95_latency_ms": 100},
            {"target_requests_per_second": 1, "p95_latency_ms": float("inf")},
            {"target_requests_per_second": 1, "p95_latency_ms": 100, "target_utilization": 1},
            {"target_requests_per_second": 1, "p95_latency_ms": 100, "burst_factor": 0},
            {"target_requests_per_second": 1, "p95_latency_ms": 100, "max_workers": True},
        ]
        for index, kwargs in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.planner.estimate(**kwargs)

    def test_invalid_samples_and_empty_latency_fail_closed(self):
        cases = [
            ([], 10),
            ([{"success": True}], 10),
            ([{"success": 1, "latency_ms": 10}], 10),
            ([{"success": True, "latency_ms": float("nan")}], 10),
            ([{"success": True, "latency_ms": 10, "timed_out": "no"}], 10),
            ("not-samples", 10),
        ]
        for index, (samples, window) in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.planner.summarize_samples(samples, window_seconds=window)

    def test_summary_rejects_invalid_window(self):
        for window in (0, -1, float("inf"), True):
            with self.subTest(window=window), self.assertRaises(ValueError):
                self.planner.summarize_samples(
                    [{"success": True, "latency_ms": 10}], window_seconds=window
                )


if __name__ == "__main__":
    unittest.main()
