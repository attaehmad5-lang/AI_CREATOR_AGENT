"""Provider SLO policy, persistence and error-budget tests."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.provider_slo_manager import ProviderSLOManager


class ProviderSLOManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "slo.json"
        self.now = [datetime(2026, 10, 16, 12, tzinfo=timezone.utc)]
        self.manager = ProviderSLOManager(self.path, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def define(self, **kwargs):
        args = {
            "slo_id": "image-availability", "service_id": "image-generation",
            "target_success_rate": 0.9, "window_seconds": 3600,
            "min_samples": 3, "max_p95_latency_ms": 500,
        }
        args.update(kwargs)
        return self.manager.define_slo(**args)

    def record(self, request_id, success=True, latency=100):
        return self.manager.record_observation(
            slo_id="image-availability", service_id="image-generation",
            request_id=request_id, success=success, latency_ms=latency,
        )

    def test_insufficient_data_does_not_claim_slo_met(self):
        self.define()
        self.record("req-1")
        result = self.manager.evaluate(slo_id="image-availability", service_id="image-generation")
        self.assertEqual(result["slo_status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["sample_count"], 1)

    def test_slo_met_reports_success_rate_latency_and_error_budget(self):
        self.define()
        self.record("req-1", True, 100)
        self.record("req-2", True, 200)
        self.record("req-3", True, 300)
        result = self.manager.evaluate(slo_id="image-availability", service_id="image-generation")
        self.assertEqual(result["slo_status"], "SLO_MET")
        self.assertEqual(result["success_rate"], 1.0)
        self.assertEqual(result["p95_latency_ms"], 300)
        self.assertAlmostEqual(result["error_budget_allowed_failures"], 0.3)
        self.assertAlmostEqual(result["error_budget_remaining"], 0.3)

    def test_failure_or_latency_breach_marks_slo_breached(self):
        self.define(target_success_rate=0.9, max_p95_latency_ms=250)
        self.record("req-1", True, 100)
        self.record("req-2", False, 200)
        self.record("req-3", True, 900)
        result = self.manager.evaluate(slo_id="image-availability", service_id="image-generation")
        self.assertEqual(result["slo_status"], "SLO_BREACHED")
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["p95_latency_ms"], 900)
        self.assertEqual(result["error_budget_remaining"], 0)

    def test_observation_is_idempotent_and_conflicts_are_rejected(self):
        self.define()
        self.assertEqual(self.record("req-1")["status"], "SLO_OBSERVATION_RECORDED")
        self.assertEqual(self.record("req-1")["status"], "ALREADY_RECORDED")
        self.assertEqual(self.record("req-1", success=False)["status"], "OBSERVATION_CONFLICT")

    def test_old_observations_are_excluded_from_window(self):
        self.define(min_samples=1)
        self.now[0] -= timedelta(hours=2)
        self.record("old", success=False, latency=999)
        self.now[0] += timedelta(hours=2)
        self.record("new", success=True, latency=100)
        result = self.manager.evaluate(slo_id="image-availability", service_id="image-generation")
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["slo_status"], "SLO_MET")

    def test_pause_and_service_scope_are_enforced(self):
        self.define()
        self.assertEqual(self.record("req-1")["status"], "SLO_OBSERVATION_RECORDED")
        self.assertEqual(self.manager.set_status(slo_id="image-availability", status="PAUSED")["status"], "SLO_STATUS_UPDATED")
        paused = self.record("req-2")
        self.assertEqual(paused["status"], "SLO_NOT_ACTIVE")
        self.assertEqual(
            self.manager.evaluate(slo_id="image-availability", service_id="another-service")["status"],
            "SERVICE_SCOPE_MISMATCH",
        )
        self.assertEqual(
            self.manager.evaluate(slo_id="image-availability", service_id="image-generation")["slo_status"],
            "PAUSED",
        )

    def test_invalid_definitions_and_metrics_fail_closed(self):
        self.assertFalse(self.define(target_success_rate=1.2)["saved"])
        self.assertFalse(self.define(window_seconds=10)["saved"])
        self.assertTrue(self.define()["saved"])
        self.assertFalse(self.record("bad", latency=float("nan"))["saved"])
        self.assertFalse(self.record("bad-success", success=1)["saved"])

    def test_duplicate_slo_id_and_observation_limit_are_handled(self):
        self.assertTrue(self.define()["saved"])
        self.assertEqual(self.define()["status"], "SLO_ALREADY_EXISTS")
        limited = ProviderSLOManager(self.path, max_observations_per_slo=3, clock=lambda: self.now[0])
        self.assertTrue(limited.record_observation(
            slo_id="image-availability", service_id="image-generation",
            request_id="req-1", success=True, latency_ms=1,
        )["saved"] is True)
        limited.data["slos"]["image-availability"]["observations"] = [
            {"request_id": f"seed-{i}", "observed_at": self.now[0].isoformat(), "success": True, "latency_ms": 1}
            for i in range(3)
        ]
        limited.path.write_text(__import__("json").dumps(limited.data), encoding="utf-8")
        result = limited.record_observation(
            slo_id="image-availability", service_id="image-generation",
            request_id="req-new", success=True, latency_ms=1,
        )
        self.assertEqual(result["status"], "OBSERVATION_LIMIT_REACHED")

    def test_state_persists_and_reloads(self):
        self.define()
        self.record("req-1", True, 50)
        reloaded = ProviderSLOManager(self.path, clock=lambda: self.now[0])
        result = reloaded.evaluate(slo_id="image-availability", service_id="image-generation")
        self.assertEqual(result["sample_count"], 1)

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderSLOManager(self.path, clock=lambda: self.now[0])


if __name__ == "__main__":
    unittest.main()
