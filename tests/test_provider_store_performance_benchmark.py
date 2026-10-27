import unittest
from pathlib import Path

from projects.provider_store.advanced.performance_benchmark import run_benchmark


class ProviderStorePerformanceBenchmarkTests(unittest.TestCase):
    def test_benchmark_runs_real_catalog_validation_and_reports_scope(self):
        result = run_benchmark(iterations=8, workers=4, max_seconds=5.0)
        self.assertTrue(result["valid"])
        self.assertEqual(result["iterations"], 8)
        self.assertEqual(result["workers"], 4)
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertGreater(result["central_provider_count"], 0)
        self.assertEqual(result["advanced_provider_count"], 18)
        self.assertEqual(result["category_count"], 6)
        self.assertIn("metadata validation only", result["scope_limit"])

    def test_benchmark_rejects_invalid_iteration_limits(self):
        for value in (0, -1, 10001, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run_benchmark(iterations=value)

    def test_benchmark_rejects_invalid_worker_limits(self):
        for value in (0, -1, 33, True, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run_benchmark(iterations=1, workers=value)

    def test_benchmark_rejects_nonpositive_time_budget(self):
        for value in (0, -1, float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    run_benchmark(iterations=1, max_seconds=value)


if __name__ == "__main__":
    unittest.main()
