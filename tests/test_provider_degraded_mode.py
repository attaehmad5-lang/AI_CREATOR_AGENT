"""Degraded-mode policy fail-closed and persistence tests."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager


class ProviderDegradedModeManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "degraded.json"
        self.manager = ProviderDegradedModeManager(
            self.path, clock=lambda: datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        )

    def tearDown(self):
        self.tmp.cleanup()

    def set_mode(self, mode, providers=None):
        return self.manager.set_mode(
            project_id="film", service_id="image",
            mode=mode, reason_code="PROVIDER_OUTAGE",
            allowed_providers=[] if providers is None else providers,
        )

    def evaluate(self, operation="provider.generate", provider=None):
        return self.manager.evaluate(
            project_id="film", service_id="image",
            operation=operation, provider_id=provider,
        )

    def test_missing_policy_fails_closed(self):
        result = self.evaluate()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "DEGRADED_MODE_NOT_CONFIGURED")

    def test_normal_mode_allows_generation(self):
        self.assertTrue(self.set_mode("NORMAL")["saved"])
        self.assertTrue(self.evaluate()["allowed"])
        self.assertTrue(self.evaluate(provider="image")["allowed"])

    def test_degraded_mode_requires_allowlisted_provider(self):
        self.assertTrue(self.set_mode("DEGRADED", ["image"])["saved"])
        precheck = self.evaluate()
        self.assertTrue(precheck["allowed"])
        self.assertTrue(precheck["provider_check_required"])
        self.assertTrue(self.evaluate(provider="image")["allowed"])
        denied = self.evaluate(provider="voice")
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["status"], "OPERATION_BLOCKED_BY_DEGRADED_MODE")

    def test_read_only_and_fail_closed_modes_block_generation(self):
        self.set_mode("READ_ONLY")
        self.assertFalse(self.evaluate()["allowed"])
        self.assertTrue(self.evaluate("provider.read")["allowed"])
        self.set_mode("FAIL_CLOSED")
        self.assertFalse(self.evaluate("provider.read")["allowed"])
        self.assertFalse(self.evaluate("provider.cancel")["allowed"])

    def test_invalid_mode_and_empty_degraded_allowlist_are_rejected(self):
        self.assertFalse(self.set_mode("DEGRADED", [])["saved"])
        self.assertFalse(self.set_mode("UNKNOWN")["saved"])
        self.assertFalse(self.manager.set_mode(
            project_id="film", service_id="image", mode=[],
            reason_code="INVALID_MODE",
        )["saved"])
        self.assertEqual(
            self.manager.evaluate(project_id=[], service_id="image", operation="provider.generate")["status"],
            "INVALID_DEGRADED_MODE_REQUEST",
        )
        self.assertEqual(
            self.manager.evaluate(project_id="film", service_id="image", operation=[])["status"],
            "INVALID_DEGRADED_MODE_REQUEST",
        )

    def test_policy_persists_across_reload(self):
        self.set_mode("DEGRADED", ["image"])
        reloaded = ProviderDegradedModeManager(self.path)
        self.assertTrue(reloaded.evaluate(
            project_id="film", service_id="image",
            operation="provider.generate", provider_id="image",
        )["allowed"])

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderDegradedModeManager(self.path)


if __name__ == "__main__":
    unittest.main()
