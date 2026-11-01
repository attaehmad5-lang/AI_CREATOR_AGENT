"""Provider incident deduplication and lifecycle tests."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_incident_manager import ProviderIncidentManager


class ProviderIncidentManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "incidents.json"
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        self.manager = ProviderIncidentManager(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_repeated_failure_updates_one_incident_without_raw_message(self):
        first = self.manager.record(
            provider_id="image", category="generation", severity="warning",
            code="PROVIDER_TIMEOUT",
        )
        second = self.manager.record(
            provider_id="image", category="generation", severity="critical",
            code="PROVIDER_TIMEOUT",
        )
        self.assertEqual(first["status"], "INCIDENT_OPENED")
        self.assertEqual(second["status"], "INCIDENT_UPDATED")
        incident = second["incident"]
        self.assertEqual(incident["occurrences"], 2)
        self.assertEqual(incident["severity"], "critical")
        self.assertNotIn("raw", repr(incident))

    def test_acknowledge_resolve_and_reopen_after_new_failure(self):
        first = self.manager.record(
            provider_id="image", category="billing", severity="critical",
            code="CREDITS_EXHAUSTED",
        )["incident"]
        self.assertEqual(self.manager.acknowledge(first["incident_id"])["status"], "INCIDENT_ACKNOWLEDGED")
        self.assertEqual(self.manager.resolve(first["incident_id"])["status"], "INCIDENT_RESOLVED")
        reopened = self.manager.record(
            provider_id="image", category="billing", severity="critical",
            code="CREDITS_EXHAUSTED",
        )
        self.assertEqual(reopened["status"], "INCIDENT_OPENED")
        self.assertEqual(reopened["incident"]["state"], "OPEN")

    def test_state_persists_and_snapshot_counts_states(self):
        incident = self.manager.record(
            provider_id="video", category="timeout", severity="error",
            code="WORKER_TIMEOUT",
        )["incident"]
        reloaded = ProviderIncidentManager(self.path, clock=lambda: self.now)
        self.assertEqual(reloaded.snapshot()["counts"]["OPEN"], 1)
        reloaded.acknowledge(incident["incident_id"])
        self.assertEqual(reloaded.snapshot()["counts"]["ACKNOWLEDGED"], 1)

    def test_incident_capacity_rejects_new_keys_but_allows_deduplicated_updates(self):
        limited = ProviderIncidentManager(
            self.path, clock=lambda: self.now, max_incidents=1
        )
        first = limited.record(
            provider_id="image", category="generation",
            severity="warning", code="PROVIDER_TIMEOUT",
        )
        duplicate = limited.record(
            provider_id="image", category="generation",
            severity="critical", code="PROVIDER_TIMEOUT",
        )
        second = limited.record(
            provider_id="video", category="generation",
            severity="error", code="PROVIDER_TIMEOUT",
        )
        self.assertEqual(first["status"], "INCIDENT_OPENED")
        self.assertEqual(duplicate["status"], "INCIDENT_UPDATED")
        self.assertEqual(second["status"], "INCIDENT_CAPACITY_REACHED")
        self.assertEqual(len(limited.data["incidents"]), 1)

    def test_incident_state_rejects_unsupported_version_and_invalid_occurrence_count(self):
        self.path.write_text(json.dumps({
            "version": "2.0", "incidents": {}
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incident state schema"):
            ProviderIncidentManager(self.path, clock=lambda: self.now)

        self.path.write_text(json.dumps({
            "version": "1.0",
            "incidents": {
                "a" * 64: {
                    "state": "OPEN", "severity": "error",
                    "provider_id": "image", "code": "TIMEOUT",
                    "occurrences": True,
                }
            },
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incident record"):
            ProviderIncidentManager(self.path, clock=lambda: self.now)

    def test_invalid_incident_fields_fail_closed(self):
        self.assertEqual(self.manager.record(
            provider_id="", category="generation", severity="error", code="X"
        )["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(self.manager.record(
            provider_id="image", category="generation", severity="critical",
            code="raw exception with spaces",
        )["status"], "INVALID_INCIDENT_CODE")


if __name__ == "__main__":
    unittest.main()
