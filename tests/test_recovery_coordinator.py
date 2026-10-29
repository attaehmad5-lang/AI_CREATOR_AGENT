"""Regression tests for cooldown-aware provider recovery rechecks."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.recovery_coordinator import ProviderRecoveryCoordinator


class FakeHealthManager:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.calls = []

    def active_probe(self, provider_id, probe, *, timeout_seconds=5.0):
        self.calls.append(provider_id)
        try:
            result = probe(timeout_seconds=timeout_seconds)
        except Exception:
            return {"provider": provider_id, "status": "NETWORK_ERROR", "verified": False}
        return {
            "provider": provider_id,
            "status": "HEALTHY" if result is True else "PROBE_FAILED",
            "verified": result is True,
        }


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "health.json"
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        self.write_state({
            "provider-a": {
                "status": "UNHEALTHY",
                "last_failure_at": (self.now - timedelta(seconds=120)).isoformat(),
            }
        })
        self.manager = FakeHealthManager(self.path)

    def write_state(self, providers):
        self.path.write_text(json.dumps({"version": "1.0", "providers": providers}), encoding="utf-8")

    def make(self, probes=None, cooldown=60):
        return ProviderRecoveryCoordinator(
            self.manager,
            probes or {"provider-a": lambda **kwargs: True},
            cooldown_seconds=cooldown,
            clock=lambda: self.now,
        )

    def test_due_unhealthy_provider_is_rechecked(self):
        result = self.make().run_due_rechecks()
        self.assertEqual(result["probed"], ["provider-a"])
        self.assertTrue(result["results"]["provider-a"]["verified"])
        self.assertEqual(self.manager.calls, ["provider-a"])

    def test_cooldown_prevents_probe(self):
        self.write_state({"provider-a": {
            "status": "UNHEALTHY",
            "last_failure_at": (self.now - timedelta(seconds=10)).isoformat(),
        }})
        result = self.make(cooldown=60).run_due_rechecks()
        self.assertEqual(result["skipped"]["provider-a"], "COOLDOWN_ACTIVE")
        self.assertEqual(self.manager.calls, [])

    def test_healthy_provider_is_not_reprobed(self):
        self.write_state({"provider-a": {
            "status": "HEALTHY",
            "last_failure_at": (self.now - timedelta(seconds=120)).isoformat(),
        }})
        result = self.make().run_due_rechecks()
        self.assertEqual(result["skipped"]["provider-a"], "NOT_RECOVERY_CANDIDATE")
        self.assertEqual(self.manager.calls, [])

    def test_future_failure_timestamp_fails_closed_for_provider(self):
        self.write_state({"provider-a": {
            "status": "DEGRADED",
            "last_failure_at": (self.now + timedelta(seconds=1)).isoformat(),
        }})
        result = self.make().run_due_rechecks()
        self.assertEqual(result["skipped"]["provider-a"], "FUTURE_FAILURE_TIMESTAMP")
        self.assertEqual(self.manager.calls, [])

    def test_duplicate_keys_and_nonfinite_snapshot_values_block_probes(self):
        for raw in (
            '{"version":"1","providers":{"provider-a":{"status":"UNHEALTHY","status":"HEALTHY"}}}',
            '{"version":"1","providers":{},"value":NaN}',
        ):
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                result = self.make().run_due_rechecks()
                self.assertEqual(result["status"], "BLOCKED_INVALID_HEALTH_STATE")
                self.assertEqual(self.manager.calls, [])

    def test_oversized_snapshot_blocks_probes(self):
        self.path.write_bytes(b'{"version":"1","providers":{}}' + b" " * (2 * 1024 * 1024))
        result = self.make().run_due_rechecks()
        self.assertEqual(result["status"], "BLOCKED_INVALID_HEALTH_STATE")
        self.assertEqual(self.manager.calls, [])

    def test_huge_cooldown_is_rejected_without_overflow(self):
        with self.assertRaises(ValueError):
            self.make(cooldown=10**1000)

    def test_malformed_snapshot_blocks_all_probes(self):
        self.path.write_text("{", encoding="utf-8")
        result = self.make().run_due_rechecks()
        self.assertEqual(result["status"], "BLOCKED_INVALID_HEALTH_STATE")
        self.assertEqual(self.manager.calls, [])

    def test_probe_exception_is_recorded_without_exception_text(self):
        def broken(**kwargs):
            raise RuntimeError("secret prompt or credential must not leak")
        result = self.make({"provider-a": broken}).run_due_rechecks()
        self.assertEqual(result["results"]["provider-a"]["status"], "NETWORK_ERROR")
        self.assertNotIn("secret prompt", json.dumps(result))

    def test_malformed_probe_result_is_not_passed_through(self):
        class MalformedHealthManager(FakeHealthManager):
            def active_probe(self, provider_id, probe, *, timeout_seconds=5.0):
                return {
                    "provider": provider_id,
                    "status": "HEALTHY\\nINJECTED",
                    "verified": "yes",
                    "detail": "sensitive diagnostic",
                }

        manager = MalformedHealthManager(self.path)
        coordinator = ProviderRecoveryCoordinator(
            manager,
            {"provider-a": lambda **kwargs: True},
            cooldown_seconds=60,
            clock=lambda: self.now,
        )
        result = coordinator.run_due_rechecks()
        self.assertEqual(
            result["results"]["provider-a"],
            {"status": "INVALID_PROBE_RESULT", "verified": False},
        )
        self.assertNotIn("sensitive diagnostic", json.dumps(result))

    def test_invalid_probe_configuration_rejected(self):
        with self.assertRaises(ValueError):
            self.make({"bad provider id": lambda **kwargs: True})
        with self.assertRaises(ValueError):
            self.make({"provider-a": None})

    def test_negative_cooldown_rejected(self):
        with self.assertRaises(ValueError):
            self.make(cooldown=-1)


if __name__ == "__main__":
    unittest.main()
