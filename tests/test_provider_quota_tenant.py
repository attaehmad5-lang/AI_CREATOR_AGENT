"""Tenant-scoped quota reservation, usage and authorization regression tests."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.quota_manager import ProviderQuotaManager


class ProviderQuotaTenantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "quota.json"
        self.now = [datetime(2026, 11, 2, 12, tzinfo=timezone.utc)]
        self.manager = ProviderQuotaManager(
            self.path,
            limits={"image": {"hourly": 5, "daily": 10, "monthly": 50}},
            tenant_limits={
                "tenant-a": {"image": {"hourly": 1, "daily": 2}},
                "tenant-b": {"image": {"hourly": 2, "daily": 4}},
            },
            clock=lambda: self.now[0],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_tenant_specific_limits_are_enforced_without_changing_global_ceiling(self):
        first = self.manager.reserve(
            provider_id="image", request_id="tenant-a-1", tenant_id="tenant-a",
        )
        blocked = self.manager.reserve(
            provider_id="image", request_id="tenant-a-2", tenant_id="tenant-a",
        )
        other_tenant = self.manager.reserve(
            provider_id="image", request_id="tenant-b-1", tenant_id="tenant-b",
        )
        unscoped = self.manager.reserve(
            provider_id="image", request_id="global-1", tenant_id="tenant-c",
        )
        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(blocked["status"], "TENANT_QUOTA_EXCEEDED")
        self.assertEqual(blocked["limit"], 1)
        self.assertEqual(other_tenant["status"], "RESERVED")
        self.assertEqual(unscoped["status"], "RESERVED")

    def test_tenant_usage_isolated_from_other_tenants_and_unscoped_records(self):
        self.manager.reserve(provider_id="image", request_id="a1", tenant_id="tenant-a")
        self.manager.reserve(provider_id="image", request_id="b1", tenant_id="tenant-b")
        self.manager.reserve(provider_id="image", request_id="global", tenant_id="tenant-c")
        self.assertEqual(
            self.manager.tenant_usage(tenant_id="tenant-a", provider_id="image"),
            {"hourly": 1, "daily": 1, "monthly": 1},
        )
        self.assertEqual(
            self.manager.tenant_usage(tenant_id="tenant-b", provider_id="image"),
            {"hourly": 1, "daily": 1, "monthly": 1},
        )
        self.assertEqual(self.manager.tenant_usage(tenant_id="../bad", provider_id="image"), {})

    def test_commit_and_release_require_matching_tenant_context(self):
        self.manager.reserve(provider_id="image", request_id="a1", tenant_id="tenant-a")
        denied_commit = self.manager.commit(request_id="a1", tenant_id="tenant-b")
        self.assertEqual(denied_commit["status"], "TENANT_ISOLATION_DENIED")
        denied_unscoped_commit = self.manager.commit(request_id="a1")
        self.assertEqual(denied_unscoped_commit["status"], "TENANT_ISOLATION_DENIED")
        self.assertEqual(
            self.manager.commit(request_id="a1", tenant_id="tenant-a")["status"],
            "COMMITTED",
        )
        denied_release = self.manager.release(
            request_id="a1", provider_id="image", tenant_id="tenant-b",
        )
        self.assertEqual(denied_release["status"], "TENANT_ISOLATION_DENIED")
        self.assertEqual(
            self.manager.release(
                request_id="a1", provider_id="image", tenant_id="tenant-a",
            )["status"],
            "ALREADY_COMMITTED",
        )

    def test_reservation_idempotency_cannot_cross_tenant_boundaries(self):
        self.manager.reserve(provider_id="image", request_id="shared", tenant_id="tenant-a")
        for tenant_id, expected in (
            (None, "TENANT_CONTEXT_REQUIRED"),
            ("tenant-b", "TENANT_ISOLATION_DENIED"),
        ):
            result = self.manager.reserve(
                provider_id="image", request_id="shared", tenant_id=tenant_id,
            )
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["reserved"])

    def test_released_tenant_reservation_keeps_its_tenant_binding_on_retry(self):
        self.manager.reserve(provider_id="image", request_id="retry", tenant_id="tenant-a")
        self.assertEqual(
            self.manager.release(
                request_id="retry", provider_id="image", tenant_id="tenant-a",
            )["status"],
            "RELEASED",
        )
        self.assertEqual(
            self.manager.reserve(
                provider_id="image", request_id="retry", tenant_id="tenant-a",
            )["status"],
            "ALREADY_RELEASED",
        )
        self.assertEqual(
            self.manager.reserve(
                provider_id="image", request_id="retry", tenant_id="tenant-b",
            )["status"],
            "TENANT_ISOLATION_DENIED",
        )

    def test_tenant_quota_defaults_to_global_provider_limit_when_no_override(self):
        result = self.manager.reserve(
            provider_id="image", request_id="tenant-c-1", tenant_id="tenant-c",
        )
        self.assertEqual(result["status"], "RESERVED")
        self.assertEqual(
            self.manager.tenant_usage(tenant_id="tenant-c", provider_id="image")["hourly"],
            1,
        )

    def test_concurrent_reservations_cannot_exceed_tenant_limit(self):
        def reserve(index):
            return self.manager.reserve(
                provider_id="image", request_id=f"parallel-{index}",
                tenant_id="tenant-a",
            )["status"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(results.count("RESERVED"), 1)
        self.assertEqual(results.count("TENANT_QUOTA_EXCEEDED"), 7)
        self.assertEqual(
            self.manager.tenant_usage(tenant_id="tenant-a", provider_id="image")["hourly"],
            1,
        )

    def test_tenant_quota_policy_rejects_invalid_ids_unknown_providers_and_raised_limits(self):
        base = {
            "limits": {"image": {"hourly": 5}},
        }
        invalid = (
            {"../bad": {"image": {"hourly": 1}}},
            {"tenant-a": {"unknown": {"hourly": 1}}},
            {"tenant-a": {"image": {"daily": 1}}},
            {"tenant-a": {"image": {"hourly": 6}}},
        )
        for policy in invalid:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    ProviderQuotaManager(
                        Path(self.temp.name) / "invalid.json",
                        **base,
                        tenant_limits=policy,
                    )

    def test_invalid_tenant_id_is_rejected(self):
        result = self.manager.reserve(
            provider_id="image", request_id="bad", tenant_id="../escape",
        )
        self.assertEqual(result["status"], "INVALID_TENANT_ID")
        self.assertEqual(
            self.manager.commit(request_id="bad", tenant_id="../escape")["status"],
            "INVALID_TENANT_ID",
        )

    def test_existing_tenantless_quota_records_remain_compatible(self):
        legacy_manager = ProviderQuotaManager(
            self.path,
            limits={"image": {"hourly": 5, "daily": 10, "monthly": 50}},
            clock=lambda: self.now[0],
        )
        result = legacy_manager.reserve(provider_id="image", request_id="legacy")
        self.assertEqual(result["status"], "RESERVED")
        self.assertIsNone(legacy_manager.data["requests"]["legacy"].get("tenant_id"))
        reloaded = ProviderQuotaManager(
            self.path,
            limits={"image": {"hourly": 5, "daily": 10, "monthly": 50}},
            tenant_limits={
                "tenant-a": {"image": {"hourly": 1, "daily": 2}},
                "tenant-b": {"image": {"hourly": 2, "daily": 4}},
            },
            clock=lambda: self.now[0],
        )
        self.assertEqual(reloaded.commit(request_id="legacy")["status"], "COMMITTED")
        self.assertEqual(
            reloaded.commit(request_id="legacy", tenant_id="tenant-a")["status"],
            "TENANT_ISOLATION_DENIED",
        )


if __name__ == "__main__":
    unittest.main()
