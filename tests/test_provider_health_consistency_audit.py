import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.health_manager import ProviderHealthManager


class ProviderHealthConsistencyAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "health.json"
        self.now = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
        self.manager = ProviderHealthManager(
            self.path,
            failure_threshold=3,
            clock=lambda: self.now,
        )

    def _write(self, providers):
        self.path.write_text(
            json.dumps({"version": "1.0", "providers": providers}),
            encoding="utf-8",
        )

    def test_empty_state_is_consistent(self):
        result = self.manager.audit_consistency()
        self.assertEqual(result["status"], "HEALTH_STATE_CONSISTENT")
        self.assertTrue(result["consistent"])
        self.assertEqual(result["providers_checked"], 0)
        self.assertEqual(result["issues"], [])

    def test_healthy_status_with_failure_counter_is_flagged(self):
        self._write({
            "provider-a": {
                "status": "HEALTHY",
                "consecutive_failures": 1,
                "consecutive_successes": 1,
                "recovery_pending": False,
                "history": [],
            }
        })
        result = self.manager.audit_consistency()
        self.assertFalse(result["consistent"])
        self.assertIn(
            "HEALTHY_COUNTERS_CONFLICT",
            [issue["code"] for issue in result["issues"]],
        )

    def test_unhealthy_status_below_threshold_is_flagged(self):
        self._write({
            "provider-a": {
                "status": "UNHEALTHY",
                "consecutive_failures": 2,
                "consecutive_successes": 0,
                "recovery_pending": False,
                "history": [],
            }
        })
        result = self.manager.audit_consistency()
        self.assertIn(
            "UNHEALTHY_BELOW_FAILURE_THRESHOLD",
            [issue["code"] for issue in result["issues"]],
        )

    def test_future_health_timestamp_is_flagged_without_repair(self):
        future = "2026-10-27T08:00:01+00:00"
        self._write({
            "provider-a": {
                "status": "HEALTHY",
                "consecutive_failures": 0,
                "consecutive_successes": 1,
                "recovery_pending": False,
                "last_checked_at": future,
                "history": [],
            }
        })
        result = self.manager.audit_consistency()
        self.assertIn(
            "FUTURE_HEALTH_TIMESTAMP",
            [issue["code"] for issue in result["issues"]],
        )
        persisted = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["providers"]["provider-a"]["last_checked_at"], future)

    def test_non_chronological_history_is_flagged(self):
        self._write({
            "provider-a": {
                "status": "HEALTHY",
                "consecutive_failures": 0,
                "consecutive_successes": 1,
                "recovery_pending": False,
                "history": [
                    {"success": True, "timestamp": "2026-10-27T07:59:00+00:00"},
                    {"success": False, "timestamp": "2026-10-27T07:58:00+00:00"},
                ],
            }
        })
        result = self.manager.audit_consistency()
        self.assertIn(
            "HISTORY_NOT_CHRONOLOGICAL",
            [issue["code"] for issue in result["issues"]],
        )


if __name__ == "__main__":
    unittest.main()
