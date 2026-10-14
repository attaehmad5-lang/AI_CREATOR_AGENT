import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.quota_manager import ProviderQuotaManager


def _reserve_quota_in_process(args):
    state_path, request_id = args
    fixed_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    manager = ProviderQuotaManager(
        state_path,
        limits={"image": {"hourly": 3}},
        clock=lambda: fixed_now,
    )
    return manager.reserve(
        provider_id="image", request_id=request_id
    )["status"]


class ProviderQuotaManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "quota.json"
        self.now = [datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)]
        self.limits = {"image": {"hourly": 2, "daily": 3, "monthly": 5}}
        self.manager = ProviderQuotaManager(
            self.path, limits=self.limits, clock=lambda: self.now[0]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_only_quota_check_does_not_create_reservations(self):
        preview = self.manager.check_available(
            provider_id="image", request_id="simulation-1"
        )
        self.assertEqual(preview["status"], "AVAILABLE")
        self.assertTrue(preview["available"])
        self.assertEqual(preview["usage"]["hourly"], 0)
        self.assertEqual(self.manager.data["requests"], {})
        self.assertFalse(self.path.exists())

    def test_read_only_quota_check_reports_exhaustion_and_duplicate_ids(self):
        self.manager.reserve(provider_id="image", request_id="job-1")
        self.manager.reserve(provider_id="image", request_id="job-2")
        exhausted = self.manager.check_available(
            provider_id="image", request_id="simulation-3"
        )
        self.assertEqual(exhausted["status"], "QUOTA_EXCEEDED")
        self.assertFalse(exhausted["available"])
        duplicate = self.manager.check_available(
            provider_id="image", request_id="job-1"
        )
        self.assertEqual(duplicate["status"], "REQUEST_ID_ALREADY_EXISTS")
        self.assertFalse(duplicate["available"])
        self.assertEqual(set(self.manager.data["requests"]), {"job-1", "job-2"})

    def test_reserve_commit_and_duplicate_requests_are_idempotent(self):
        first = self.manager.reserve(provider_id="image", request_id="job-1")
        duplicate = self.manager.reserve(provider_id="image", request_id="job-1")
        committed = self.manager.commit(request_id="job-1")
        committed_again = self.manager.commit(request_id="job-1")
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(duplicate["status"], "ALREADY_RESERVED")
        self.assertEqual(committed["status"], "COMMITTED")
        self.assertEqual(committed_again["status"], "ALREADY_COMMITTED")
        self.assertEqual(self.manager.usage(provider_id="image")["hourly"], 1)

    def test_quota_limits_count_reservations_and_committed_requests(self):
        self.manager.reserve(provider_id="image", request_id="job-1")
        self.manager.reserve(provider_id="image", request_id="job-2")
        result = self.manager.reserve(provider_id="image", request_id="job-3")
        self.assertEqual(result["status"], "QUOTA_EXCEEDED")
        self.assertEqual(result["period"], "hourly")
        self.assertEqual(result["used"], 2)

    def test_release_frees_quota_capacity(self):
        self.manager.reserve(provider_id="image", request_id="job-1")
        self.manager.reserve(provider_id="image", request_id="job-2")
        self.assertEqual(self.manager.release(request_id="job-1")["status"], "RELEASED")
        self.assertEqual(self.manager.data["requests"]["job-1"]["status"], "RELEASED")
        self.assertEqual(self.manager.reserve(provider_id="image", request_id="job-1")["status"], "ALREADY_RELEASED")
        self.assertEqual(self.manager.reserve(provider_id="image", request_id="job-3")["status"], "RESERVED")

    def test_concurrent_reservations_never_exceed_limit(self):
        def reserve(index):
            return self.manager.reserve(provider_id="image", request_id=f"parallel-{index}")
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(40)))
        self.assertEqual(sum(item["status"] == "RESERVED" for item in results), 2)
        self.assertEqual(sum(item["status"] == "QUOTA_EXCEEDED" for item in results), 38)

    def test_concurrent_reservations_across_processes_never_exceed_limit(self):
        args = [
            (str(self.path), f"process-job-{index}")
            for index in range(20)
        ]
        with ProcessPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_reserve_quota_in_process, args))

        self.assertEqual(statuses.count("RESERVED"), 3)
        self.assertEqual(statuses.count("QUOTA_EXCEEDED"), 17)
        self.assertEqual(self.manager.usage(provider_id="image")["hourly"], 3)

    def test_state_persists_and_old_windows_expire(self):
        self.manager.reserve(provider_id="image", request_id="job-1")
        loaded = ProviderQuotaManager(
            self.path, limits=self.limits, clock=lambda: self.now[0]
        )
        self.assertEqual(loaded.usage(provider_id="image")["hourly"], 1)
        self.now[0] += timedelta(hours=2)
        self.assertEqual(loaded.usage(provider_id="image")["hourly"], 0)
        self.assertEqual(loaded.usage(provider_id="image")["daily"], 1)

    def test_daily_and_monthly_quotas_reset_at_utc_calendar_boundaries(self):
        self.now[0] = datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)
        self.manager.reserve(provider_id="image", request_id="before-midnight")
        self.now[0] = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(self.manager.usage(provider_id="image")["daily"], 0)
        self.assertEqual(self.manager.usage(provider_id="image")["monthly"], 0)
        self.assertEqual(self.manager.usage(provider_id="image")["hourly"], 0)

    def test_release_cannot_remove_committed_usage(self):
        self.manager.reserve(provider_id="image", request_id="job-1")
        self.manager.commit(request_id="job-1")
        self.assertEqual(self.manager.release(request_id="job-1")["status"], "ALREADY_COMMITTED")

    def test_invalid_configuration_and_unknown_provider_fail_closed(self):
        with self.assertRaises(ValueError):
            ProviderQuotaManager(self.path, limits={"image": {"daily": True}})
        with self.assertRaises(ValueError):
            ProviderQuotaManager(self.path, limits={"image": {"weekly": 10}})
        self.assertEqual(self.manager.reserve(provider_id="unknown", request_id="job")["status"], "NO_QUOTA_POLICY")
        self.assertEqual(self.manager.reserve(provider_id="", request_id="job")["status"], "INVALID_REQUEST")

    def test_corrupt_state_is_rejected(self):
        self.path.write_text(json.dumps({"requests": {"job": {"provider_id": "image", "status": "BOGUS", "created_at": "bad"}}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderQuotaManager(self.path, limits=self.limits)


if __name__ == "__main__":
    unittest.main()
