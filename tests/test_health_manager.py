import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.health_manager import ProviderHealthManager


class ProviderHealthManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "health.json"
        self.now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
        self.manager = ProviderHealthManager(
            self.path, history_limit=3, failure_threshold=2,
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_concurrent_updates_preserve_health_history_and_failure_count(self):
        manager = ProviderHealthManager(
            self.path,
            history_limit=200,
            failure_threshold=200,
            clock=lambda: self.now[0],
        )

        def record_failure(_):
            return manager.record_result("image", success=False, error_code="HTTP_503")

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(record_failure, range(100)))

        self.assertTrue(all(result["saved"] for result in results))
        record = manager.data["providers"]["image"]
        self.assertEqual(len(record["history"]), 100)
        self.assertEqual(record["consecutive_failures"], 100)

    def test_success_and_failure_history_persist(self):
        self.manager.record_result("image", success=True, latency_ms=42)
        self.manager.record_result("image", success=False, error_code="HTTP_503")
        loaded = ProviderHealthManager(self.path, clock=lambda: self.now[0])
        record = loaded.data["providers"]["image"]
        self.assertEqual(record["status"], "DEGRADED")
        self.assertEqual(len(record["history"]), 2)
        self.assertEqual(record["history"][-1]["error_code"], "HTTP_503")

    def test_failure_threshold_excludes_unhealthy_provider(self):
        self.manager.record_result("image", success=False, error_code="TIMEOUT")
        self.manager.record_result("image", success=False, error_code="TIMEOUT")
        result = self.manager.is_eligible("image")
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "UNHEALTHY")

    def test_success_recovers_provider(self):
        self.manager.record_result("image", success=False)
        self.manager.record_result("image", success=False)
        self.manager.record_result("image", success=True, latency_ms=10)
        self.assertTrue(self.manager.is_eligible("image")["eligible"])
        self.assertEqual(self.manager.data["providers"]["image"]["status"], "HEALTHY")

    def test_stale_health_is_excluded(self):
        self.manager.record_result("image", success=True, latency_ms=10)
        self.now[0] += timedelta(seconds=301)
        result = self.manager.is_eligible("image", stale_after_seconds=300)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "STALE")

    def test_latency_limit_excludes_slow_provider(self):
        self.manager.record_result("image", success=True, latency_ms=900)
        result = self.manager.is_eligible("image", max_latency_ms=500)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "SLOW")

    def test_active_probe_classifies_failure_without_leaking_details(self):
        def probe(timeout_seconds):
            raise TimeoutError("private endpoint and token")
        result = self.manager.active_probe("image", probe)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertNotIn("private endpoint and token", repr(result))

    def test_health_history_redacts_unstructured_error_details(self):
        result = self.manager.record_result(
            "image", success=False, error_code="token=do-not-log-this"
        )
        self.assertTrue(result["saved"])
        record = self.manager.data["providers"]["image"]
        self.assertEqual(record["history"][-1]["error_code"], "UNCLASSIFIED_ERROR")
        self.assertNotIn("do-not-log-this", repr(record))

    def test_history_is_bounded(self):
        for i in range(5):
            self.manager.record_result("image", success=(i % 2 == 0))
        self.assertEqual(len(self.manager.data["providers"]["image"]["history"]), 3)

    def test_invalid_manager_configuration_and_probe_timeout_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderHealthManager(self.path, history_limit=True)
        with self.assertRaises(ValueError):
            ProviderHealthManager(self.path, failure_threshold=1.5)
        with self.assertRaises(ValueError):
            ProviderHealthManager(self.path, clock="not-callable")
        self.assertEqual(
            self.manager.active_probe("image", lambda **kwargs: True, timeout_seconds=float("nan"))["status"],
            "INVALID_TIMEOUT",
        )
        self.assertEqual(
            self.manager.active_probe("image", lambda **kwargs: True, timeout_seconds=float("inf"))["status"],
            "INVALID_TIMEOUT",
        )

    def test_invalid_input_is_rejected(self):
        self.assertEqual(self.manager.record_result("", success=True)["status"], "INVALID_PROVIDER")
        self.assertEqual(self.manager.record_result("image", success=True, latency_ms=-1)["status"], "INVALID_LATENCY")
        self.assertEqual(self.manager.record_result("image", success=True, latency_ms=float("nan"))["status"], "INVALID_LATENCY")
        self.assertEqual(self.manager.record_result("image", success=True, latency_ms=float("inf"))["status"], "INVALID_LATENCY")
        self.assertEqual(self.manager.record_result("image", success=True, latency_ms=True)["status"], "INVALID_LATENCY")
        self.assertEqual(self.manager.is_eligible("image", stale_after_seconds=-1)["status"], "INVALID_HEALTH_POLICY")
        self.assertEqual(self.manager.is_eligible("image", stale_after_seconds=float("nan"))["status"], "INVALID_HEALTH_POLICY")
        self.assertEqual(self.manager.is_eligible("image", max_latency_ms=-1)["status"], "INVALID_HEALTH_POLICY")
        self.assertEqual(self.manager.is_eligible("image", max_latency_ms=float("inf"))["status"], "INVALID_HEALTH_POLICY")


if __name__ == "__main__":
    unittest.main()
