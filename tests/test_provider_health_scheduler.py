import tempfile
import unittest
from pathlib import Path

from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.health_scheduler import ProviderHealthScheduler


class ProviderHealthSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [100.0]
        self.health = ProviderHealthManager(Path(self.temp.name) / "health.json")
        self.scheduler = ProviderHealthScheduler(
            self.health,
            interval_seconds=10,
            timeout_seconds=2,
            max_providers=2,
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_runs_due_probes_with_timeout_and_respects_interval(self):
        observed = []

        def probe(*, timeout_seconds):
            observed.append(timeout_seconds)
            return True

        self.assertTrue(self.scheduler.register("image", probe)["registered"])
        first = self.scheduler.run_due(max_checks=1)
        self.assertEqual(first["status"], "HEALTH_CHECKS_COMPLETED")
        self.assertEqual(first["checked_count"], 1)
        self.assertEqual(observed, [2.0])
        self.assertEqual(self.scheduler.run_due()["status"], "NO_CHECKS_DUE")
        self.now[0] += 10
        second = self.scheduler.run_due()
        self.assertEqual(second["checked_count"], 1)
        self.assertEqual(observed, [2.0, 2.0])

    def test_batch_limit_and_capacity_are_enforced(self):
        probe = lambda *, timeout_seconds: True
        self.assertTrue(self.scheduler.register("image", probe)["registered"])
        self.assertTrue(self.scheduler.register("video", probe)["registered"])
        self.assertEqual(
            self.scheduler.register("audio", probe)["status"],
            "SCHEDULER_CAPACITY_REACHED",
        )
        result = self.scheduler.run_due(max_checks=1)
        self.assertEqual(result["checked_count"], 1)
        self.assertEqual(result["scheduled_count"], 2)

    def test_unregister_and_invalid_inputs_are_safe(self):
        self.assertEqual(self.scheduler.register("../secret", lambda **_: True)["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(self.scheduler.register("image", None)["status"], "INVALID_PROBE")
        self.assertEqual(self.scheduler.run_due(max_checks=True)["status"], "INVALID_CHECK_LIMIT")
        self.scheduler.register("image", lambda **_: True)
        self.assertEqual(self.scheduler.unregister("image")["status"], "PROBE_UNSCHEDULED")
        self.assertEqual(self.scheduler.run_due()["status"], "NO_CHECKS_DUE")

    def test_invalid_numeric_policy_and_clock_are_rejected(self):
        for value in (0, -1, True, float("nan"), float("inf"), 10**1000):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderHealthScheduler(self.health, interval_seconds=value)
        broken_clock = ProviderHealthScheduler(
            self.health, clock=lambda: float("nan")
        )
        self.assertEqual(
            broken_clock.register("image", lambda **_: True)["status"],
            "INVALID_CLOCK",
        )


if __name__ == "__main__":
    unittest.main()
