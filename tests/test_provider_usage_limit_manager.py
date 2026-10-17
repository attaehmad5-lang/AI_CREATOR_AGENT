import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_usage_limit_manager import ProviderUsageLimitManager


class ProviderUsageLimitManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.now = [datetime(2026, 10, 17, 12, tzinfo=timezone.utc)]
        self.manager = ProviderUsageLimitManager(
            Path(self.temp_dir.name) / "usage-limits.json",
            limits={
                "user": {"hourly": 3, "daily": 10, "monthly": 20},
                "project": {"hourly": 5},
                "tenant": {"daily": 8},
            },
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def reserve(self, request_id, user_id="user-a", project_id="project-a", tenant_id="tenant-a"):
        return self.manager.reserve(
            user_id=user_id, project_id=project_id,
            tenant_id=tenant_id, request_id=request_id,
        )

    def test_user_project_and_tenant_limits_are_separate(self):
        self.assertEqual(self.reserve("r1")["status"], "RESERVED")
        self.assertEqual(self.reserve("r2")["status"], "RESERVED")
        self.assertEqual(self.reserve("r3")["status"], "RESERVED")
        blocked_user = self.reserve("r4")
        self.assertEqual(blocked_user["status"], "USAGE_LIMIT_EXCEEDED")
        self.assertEqual(blocked_user["scope_type"], "user")
        self.assertEqual(blocked_user["period"], "hourly")
        other_user = self.reserve("r4", user_id="user-b")
        self.assertEqual(other_user["status"], "RESERVED")

    def test_project_and_tenant_caps_block_even_when_user_has_capacity(self):
        for i in range(3):
            self.assertEqual(self.reserve(f"project-{i}", user_id=f"user-{i}")["status"], "RESERVED")
        # The project hourly cap is five, so two more are accepted and the sixth is blocked.
        self.assertEqual(self.reserve("project-3", user_id="user-3")["status"], "RESERVED")
        self.assertEqual(self.reserve("project-4", user_id="user-4")["status"], "RESERVED")
        project_block = self.reserve("project-5", user_id="user-5")
        self.assertEqual(project_block["scope_type"], "project")
        self.assertEqual(project_block["status"], "USAGE_LIMIT_EXCEEDED")

    def test_idempotency_and_scope_conflicts_are_safe(self):
        self.assertEqual(self.reserve("same")["status"], "RESERVED")
        self.assertEqual(self.reserve("same")["status"], "ALREADY_RESERVED")
        conflict = self.reserve("same", user_id="different-user")
        self.assertEqual(conflict["status"], "REQUEST_ID_SCOPE_CONFLICT")
        self.assertTrue(self.manager.commit(request_id="same")["committed"])
        self.assertEqual(self.reserve("same")["status"], "ALREADY_COMMITTED")
        self.assertFalse(self.manager.release(request_id="same")["released"])

    def test_released_reservations_do_not_consume_future_capacity(self):
        for i in range(3):
            self.assertEqual(self.reserve(f"r{i}")["status"], "RESERVED")
        self.assertTrue(self.manager.release(request_id="r1")["released"])
        self.assertEqual(self.reserve("r4")["status"], "RESERVED")
        self.assertEqual(self.manager.reserve(
            user_id="user-a", project_id="project-a",
            tenant_id="tenant-a", request_id="r1",
        )["status"], "REQUEST_ALREADY_RELEASED")

    def test_utc_window_rollover_resets_hourly_counts(self):
        for i in range(3):
            self.assertEqual(self.reserve(f"hour-{i}")["status"], "RESERVED")
        self.now[0] = datetime(2026, 10, 17, 13, tzinfo=timezone.utc)
        self.assertEqual(self.reserve("next-hour")["status"], "RESERVED")

    def test_concurrent_reservations_never_overspend_user_limit(self):
        manager = ProviderUsageLimitManager(
            Path(self.temp_dir.name) / "concurrent.json",
            limits={"user": {"hourly": 5}},
            clock=lambda: self.now[0],
        )
        def reserve(i):
            return manager.reserve(
                user_id="shared-user", project_id=f"project-{i}",
                tenant_id=f"tenant-{i}", request_id=f"parallel-{i}",
            )
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(24)))
        self.assertEqual(sum(item["status"] == "RESERVED" for item in results), 5)
        self.assertEqual(sum(item["status"] == "USAGE_LIMIT_EXCEEDED" for item in results), 19)

    def test_invalid_limits_and_corrupt_state_fail_closed(self):
        with self.assertRaises(ValueError):
            ProviderUsageLimitManager(self.manager.path.with_name("bad-limits.json"), limits={"user": {"hourly": True}})
        self.manager.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderUsageLimitManager(self.manager.path, limits={"user": {"hourly": 5}})

    def test_invalid_scope_ids_are_rejected(self):
        result = self.manager.reserve(
            user_id="", project_id="project-a", tenant_id="tenant-a", request_id="bad",
        )
        self.assertEqual(result["status"], "INVALID_USAGE_LIMIT_REQUEST")


if __name__ == "__main__":
    unittest.main()
