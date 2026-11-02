"""Rolling-window service-level objective and error-budget tests."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_slo_manager import ProviderSLOManager


class ProviderSLOManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "slos.json"
        self.now = [datetime(2026, 11, 2, 12, tzinfo=timezone.utc)]
        self.manager = ProviderSLOManager(
            self.path, max_samples=10, clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def configure(self, **overrides):
        settings = {
            "provider_id": "image-provider",
            "category": "image",
            "availability_target": 0.95,
            "max_p95_latency_ms": 500,
            "window_seconds": 3600,
            "minimum_samples": 4,
        }
        settings.update(overrides)
        return self.manager.configure("image-slo", **settings)

    def record(self, success=True, latency=100, provider="image-provider", category="image"):
        return self.manager.record(
            provider_id=provider, category=category,
            success=success, latency_ms=latency,
        )

    def test_configured_slo_persists_and_evaluates_met_with_error_budget(self):
        self.assertEqual(self.configure()["status"], "SLO_CONFIGURED")
        for latency in (80, 100, 120, 140):
            self.assertTrue(self.record(latency=latency)["saved"])
        reloaded = ProviderSLOManager(
            self.path, clock=lambda: self.now[0],
        )
        result = reloaded.evaluate("image-slo")
        self.assertEqual(result["status"], "SLO_MET")
        evaluation = result["evaluation"]
        self.assertEqual(evaluation["sample_count"], 4)
        self.assertEqual(evaluation["availability"], 1.0)
        self.assertEqual(evaluation["p95_latency_ms"], 140)
        self.assertEqual(evaluation["error_budget_remaining_percent"], 100.0)
        self.assertEqual(evaluation["remaining_error_budget"], 0.2)

    def test_availability_or_latency_breach_is_reported(self):
        self.configure(
            availability_target=0.9,
            max_p95_latency_ms=150,
            minimum_samples=4,
        )
        for success, latency in (
            (True, 100), (True, 120), (True, 140), (False, 300),
        ):
            self.record(success=success, latency=latency)
        result = self.manager.evaluate("image-slo")
        self.assertEqual(result["status"], "SLO_BREACHED")
        evaluation = result["evaluation"]
        self.assertFalse(evaluation["availability_met"])
        self.assertFalse(evaluation["latency_met"])
        self.assertEqual(evaluation["failure_count"], 1)
        self.assertEqual(evaluation["remaining_error_budget"], 0.0)
        self.assertEqual(evaluation["error_budget_remaining_percent"], 0.0)

    def test_insufficient_samples_are_not_misreported_as_met(self):
        self.configure(minimum_samples=3)
        self.record()
        self.record()
        result = self.manager.evaluate("image-slo")
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(result["evaluation"]["availability_met"])
        self.assertIsNone(result["evaluation"]["latency_met"])

    def test_rolling_window_excludes_stale_and_future_observations(self):
        self.configure(window_seconds=60, minimum_samples=2)
        self.record()
        self.now[0] += timedelta(seconds=61)
        self.record()
        # A timestamp from a future worker clock is never counted in this window.
        self.now[0] += timedelta(seconds=1)
        self.record()
        self.now[0] += timedelta(seconds=60)
        result = self.manager.evaluate("image-slo")
        self.assertEqual(result["evaluation"]["sample_count"], 1)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_evaluation_filters_by_provider_and_category(self):
        self.configure(minimum_samples=2)
        self.record(provider="other-provider", category="image")
        self.record(provider="image-provider", category="video")
        self.record(provider="image-provider", category="image")
        result = self.manager.evaluate("image-slo")
        self.assertEqual(result["evaluation"]["sample_count"], 1)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")

    def test_sample_history_is_bounded_and_state_persists(self):
        manager = ProviderSLOManager(
            self.path, max_samples=3, clock=lambda: self.now[0],
        )
        manager.configure(
            "limited", provider_id="image-provider", category="image",
            availability_target=0.9, max_p95_latency_ms=200,
            window_seconds=3600, minimum_samples=1,
        )
        for latency in (10, 20, 30, 40, 50):
            manager.record(
                provider_id="image-provider", category="image",
                success=True, latency_ms=latency,
            )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["samples"]), 3)
        self.assertEqual(
            ProviderSLOManager(self.path, clock=lambda: self.now[0])
            .evaluate("limited")["evaluation"]["p95_latency_ms"],
            50,
        )

    def test_evaluate_all_and_objective_removal(self):
        self.configure()
        self.manager.configure(
            "video-slo", provider_id="video-provider", category="video",
            availability_target=0.99, max_p95_latency_ms=5000,
            minimum_samples=1,
        )
        result = self.manager.evaluate_all()
        self.assertEqual(result["status"], "SLO_EVALUATIONS_AVAILABLE")
        self.assertEqual(len(result["evaluations"]), 2)
        self.assertTrue(self.manager.remove("video-slo")["removed"])
        self.assertEqual(self.manager.evaluate("video-slo")["status"], "SLO_NOT_FOUND")
        self.assertEqual(self.manager.evaluate_all()["evaluations"][0]["slo_id"], "image-slo")

    def test_invalid_configuration_and_observations_fail_closed(self):
        invalid_configs = (
            {"availability_target": 0},
            {"availability_target": 1.1},
            {"availability_target": float("nan")},
            {"max_p95_latency_ms": 0},
            {"max_p95_latency_ms": float("inf")},
            {"window_seconds": 59},
            {"window_seconds": True},
            {"minimum_samples": 0},
            {"minimum_samples": False},
        )
        for override in invalid_configs:
            with self.subTest(override=override):
                settings = {
                    "provider_id": "image-provider", "category": "image",
                    "availability_target": 0.95, "max_p95_latency_ms": 500,
                    "window_seconds": 3600, "minimum_samples": 4,
                }
                settings.update(override)
                self.assertFalse(self.manager.configure("bad-slo", **settings)["saved"])
        self.assertEqual(self.manager.record(
            provider_id="image-provider", category="image",
            success="yes", latency_ms=10,
        )["status"], "INVALID_SUCCESS_FLAG")
        self.assertEqual(self.manager.record(
            provider_id="image-provider", category="image",
            success=True, latency_ms=float("nan"),
        )["status"], "INVALID_LATENCY")
        self.assertEqual(self.manager.configure(
            "bad id", provider_id="image-provider", category="image",
            availability_target=0.9, max_p95_latency_ms=100,
        )["status"], "INVALID_SLO_ID")

    def test_invalid_state_and_naive_clock_fail_closed(self):
        self.path.write_text(json.dumps({
            "version": "1.0",
            "objectives": {},
            "samples": [{
                "provider_id": "image-provider",
                "category": "image",
                "success": True,
                "latency_ms": 1,
                "recorded_at": "2026-11-02T12:00:00",
            }],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "observation record"):
            ProviderSLOManager(self.path)

        naive = ProviderSLOManager(
            Path(self.temp_dir.name) / "naive.json",
            clock=lambda: datetime(2026, 11, 2, 12),
        )
        self.assertEqual(naive.record(
            provider_id="image-provider", category="image",
            success=True, latency_ms=10,
        )["status"], "INVALID_CLOCK")
        self.assertEqual(naive.evaluate("missing")["status"], "INVALID_CLOCK")

    def test_concurrent_observations_respect_bounded_capacity(self):
        from concurrent.futures import ThreadPoolExecutor
        manager = ProviderSLOManager(
            self.path, max_samples=5, clock=lambda: self.now[0],
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda i: manager.record(
                    provider_id="image-provider", category="image",
                    success=i % 2 == 0, latency_ms=i,
                ),
                range(40),
            ))
        self.assertTrue(all(item["saved"] for item in results))
        self.assertEqual(len(manager.data["samples"]), 5)


if __name__ == "__main__":
    unittest.main()
