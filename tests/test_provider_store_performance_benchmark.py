import unittest

from projects.provider_store.advanced.performance_benchmark import run_benchmark, run_stream_benchmark


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

    def test_stream_benchmark_measures_bounded_chunk_assembly(self):
        result = run_stream_benchmark(
            iterations=4, chunks_per_stream=8, chunk_size_bytes=128,
            workers=2, max_seconds=5.0,
        )
        self.assertEqual(result["benchmark"], "bounded_stream_assembly")
        self.assertEqual(result["total_bytes"], 4096)
        self.assertTrue(result["sha256_verified"])
        self.assertGreaterEqual(result["bytes_per_second"], 0)
        self.assertIn("no provider network", result["scope_limit"])

    def test_stream_benchmark_rejects_invalid_or_excessive_inputs(self):
        for kwargs in (
            {"iterations": True},
            {"chunks_per_stream": 0},
            {"chunk_size_bytes": 0},
            {"workers": 33},
            {"chunks_per_stream": 10000, "chunk_size_bytes": 10000},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    run_stream_benchmark(**kwargs)

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
