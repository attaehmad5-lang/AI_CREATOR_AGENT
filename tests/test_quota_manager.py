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


class ScopedProviderQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "scoped-quota.json"
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        self.manager = ProviderQuotaManager(
            self.path,
            limits={"image": {"daily": 10}},
            clock=lambda: self.now,
            scope_limits={
                "tenant": {
                    "tenant-a": {"image": {"daily": 1}},
                    "tenant-b": {"image": {"daily": 1}},
                },
            },
        )

    def test_tenant_quotas_are_isolated(self):
        first = self.manager.reserve(
            provider_id="image", request_id="tenant-a-1", tenant_id="tenant-a"
        )
        exhausted = self.manager.reserve(
            provider_id="image", request_id="tenant-a-2", tenant_id="tenant-a"
        )
        other_tenant = self.manager.reserve(
            provider_id="image", request_id="tenant-b-1", tenant_id="tenant-b"
        )
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(exhausted["status"], "SCOPE_QUOTA_EXCEEDED")
        self.assertEqual(exhausted["scope"], "tenant")
        self.assertEqual(other_tenant["status"], "RESERVED")
        self.assertEqual(
            self.manager.usage_for_scope(
                scope_type="tenant", scope_id="tenant-a", provider_id="image"
            )["daily"],
            1,
        )
        self.assertEqual(
            self.manager.usage_for_scope(
                scope_type="tenant", scope_id="tenant-b", provider_id="image"
            )["daily"],
            1,
        )

    def test_configured_scope_requires_identity_and_known_policy(self):
        missing = self.manager.reserve(provider_id="image", request_id="no-tenant")
        unknown = self.manager.reserve(
            provider_id="image", request_id="unknown-tenant", tenant_id="tenant-c"
        )
        invalid = self.manager.reserve(
            provider_id="image", request_id="invalid-tenant", tenant_id=[]
        )
        self.assertEqual(missing["status"], "SCOPE_ID_REQUIRED")
        self.assertEqual(unknown["status"], "NO_SCOPE_QUOTA_POLICY")
        self.assertEqual(invalid["status"], "INVALID_SCOPE_ID")

    def test_same_request_id_cannot_cross_tenant_boundaries(self):
        first = self.manager.reserve(
            provider_id="image", request_id="shared-request", tenant_id="tenant-a"
        )
        second = self.manager.reserve(
            provider_id="image", request_id="shared-request", tenant_id="tenant-b"
        )
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(second["status"], "REQUEST_ID_CONFLICT")

    def test_user_and_project_quotas_are_independently_enforced(self):
        scoped = ProviderQuotaManager(
            self.path,
            limits={"image": {"daily": 10}},
            clock=lambda: self.now,
            scope_limits={
                "project": {
                    "project-a": {"image": {"daily": 2}},
                    "project-b": {"image": {"daily": 2}},
                },
                "user": {
                    "user-a": {"image": {"daily": 2}},
                    "user-b": {"image": {"daily": 2}},
                },
            },
        )
        first = scoped.reserve(
            provider_id="image", request_id="scoped-1",
            project_id="project-a", user_id="user-a",
        )
        second = scoped.reserve(
            provider_id="image", request_id="scoped-2",
            project_id="project-a", user_id="user-a",
        )
        project_exhausted = scoped.reserve(
            provider_id="image", request_id="scoped-3",
            project_id="project-a", user_id="user-b",
        )
        other_project = scoped.reserve(
            provider_id="image", request_id="scoped-4",
            project_id="project-b", user_id="user-b",
        )
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(second["status"], "RESERVED")
        self.assertEqual(project_exhausted["status"], "SCOPE_QUOTA_EXCEEDED")
        self.assertEqual(project_exhausted["scope"], "project")
        self.assertEqual(other_project["status"], "RESERVED")

    def test_scope_configuration_rejects_unknown_providers_and_bad_limits(self):
        with self.assertRaises(ValueError):
            ProviderQuotaManager(
                self.path,
                limits={"image": {"daily": 10}},
                scope_limits={"tenant": {"tenant-a": {"unknown": {"daily": 1}}}},
            )
        with self.assertRaises(ValueError):
            ProviderQuotaManager(
                self.path,
                limits={"image": {"daily": 10}},
                scope_limits={"tenant": {"tenant-a": {"image": {"daily": True}}}},
            )


if __name__ == "__main__":
    unittest.main()
