import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.provider_incident_manager import ProviderIncidentManager
from projects.provider_store.provider_outage_detector import ProviderOutageDetector


class ProviderOutageDetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        self.health = ProviderHealthManager(
            root / "health.json",
            failure_threshold=1,
            clock=lambda: self.now[0],
        )
        self.incidents = ProviderIncidentManager(
            root / "incidents.json",
            clock=lambda: self.now[0],
        )
        self.detector = ProviderOutageDetector(
            self.health, self.incidents, stale_after_seconds=300
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_unhealthy_provider_opens_deduplicated_incident(self):
        self.health.record_result("image", success=False, error_code="HTTP_503")
        first = self.detector.evaluate("image")
        second = self.detector.evaluate("image")
        self.assertEqual(first["status"], "OUTAGE_DETECTED")
        self.assertTrue(first["outage"])
        self.assertEqual(first["incident_status"], "INCIDENT_OPENED")
        self.assertEqual(second["incident_status"], "INCIDENT_UPDATED")
        snapshot = self.incidents.snapshot()
        self.assertEqual(snapshot["counts"]["OPEN"], 1)
        self.assertEqual(snapshot["incidents"][0]["occurrences"], 2)

    def test_recovery_resolves_open_health_incident(self):
        self.health.record_result("image", success=False, error_code="HTTP_503")
        opened = self.detector.evaluate("image")
        self.health.record_result("image", success=True, latency_ms=15)
        recovered = self.detector.evaluate("image")
        self.assertEqual(recovered["status"], "OUTAGE_CLEARED")
        self.assertEqual(recovered["resolved_incidents"], 1)
        incident = self.incidents.snapshot()["incidents"][0]
        self.assertEqual(incident["state"], "RESOLVED")
        self.assertEqual(incident["incident_id"], opened["incident_id"])

    def test_stale_health_creates_warning_incident_without_error_details(self):
        self.health.record_result("image", success=True, latency_ms=10)
        self.now[0] += timedelta(seconds=301)
        result = self.detector.evaluate("image")
        self.assertEqual(result["status"], "OUTAGE_DETECTED")
        self.assertEqual(result["health_status"], "STALE")
        self.assertEqual(
            self.incidents.snapshot()["incidents"][0]["code"],
            "PROVIDER_HEALTH_STALE",
        )

    def test_missing_health_evidence_does_not_claim_outage(self):
        result = self.detector.evaluate("image")
        self.assertEqual(result["status"], "OUTAGE_STATE_UNKNOWN")
        self.assertIsNone(result["outage"])

    def test_invalid_provider_id_and_configuration_fail_closed(self):
        self.assertEqual(self.detector.evaluate("../secret")["status"], "INVALID_PROVIDER_ID")
        with self.assertRaises(ValueError):
            ProviderOutageDetector(self.health, self.incidents, stale_after_seconds=float("nan"))


if __name__ == "__main__":
    unittest.main()
