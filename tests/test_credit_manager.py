import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.credit_manager import ProviderCreditManager


def _refresh_credit_in_process(args):
    state_path, index = args
    fixed_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    manager = ProviderCreditManager(
        state_path, clock=lambda: fixed_now
    )
    result = manager.refresh(
        f"provider-{index}",
        lambda _: {
            "verified": True,
            "free_credits_remaining": float(index + 1),
            "source": "official-account-api",
            "unit": "credits",
        },
    )
    return result["status"]


def _reserve_credit_in_process(args):
    state_path, index = args
    fixed_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    manager = ProviderCreditManager(state_path, clock=lambda: fixed_now)
    return manager.reserve(
        provider_id="shared-provider",
        amount=2.0,
        request_id=f"credit-job-{index}",
        unit="USD",
    )["status"]


class ProviderCreditManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "credits.json"
        self.now = [datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)]
        self.manager = ProviderCreditManager(
            self.path, max_age_seconds=300, clock=lambda: self.now[0]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verified_credit_snapshot_is_persisted_and_usable(self):
        result = self.manager.refresh(
            "image-provider",
            lambda provider_id: {
                "verified": True,
                "free_credits_remaining": 12.5,
                "unit": "credits",
                "source": "official-account-api",
            },
        )
        self.assertEqual(result["status"], "CREDIT_BALANCE_UPDATED")
        self.assertEqual(self.manager.usable_free_credits(), {"image-provider": 12.5})
        loaded = ProviderCreditManager(
            self.path, max_age_seconds=300, clock=lambda: self.now[0]
        )
        self.assertEqual(loaded.get_balance("image-provider")["status"], "CREDIT_BALANCE_FRESH")

    def test_concurrent_credit_refreshes_across_processes_preserve_all_snapshots(self):
        args = [(str(self.path), index) for index in range(20)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_refresh_credit_in_process, args))

        self.assertEqual(statuses.count("CREDIT_BALANCE_UPDATED"), 20)
        loaded = ProviderCreditManager(
            self.path, clock=lambda: self.now[0]
        )
        self.assertEqual(len(loaded.data["providers"]), 20)
        self.assertEqual(len(loaded.usable_free_credits()), 20)

    def test_credit_reservations_prevent_concurrent_overspend_across_processes(self):
        self.manager.refresh(
            "shared-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        args = [(str(self.path), index) for index in range(12)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_reserve_credit_in_process, args))
        self.assertEqual(statuses.count("RESERVED"), 2)
        self.assertEqual(statuses.count("INSUFFICIENT_FREE_CREDITS"), 10)
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"shared-provider": 1.0})

    def test_committed_credit_usage_is_counted_until_a_new_snapshot(self):
        self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        self.assertEqual(
            self.manager.reserve(
                provider_id="image-provider", amount=2.0, request_id="credit-job", unit="USD"
            )["status"],
            "RESERVED",
        )
        self.assertEqual(self.manager.commit(request_id="credit-job")["status"], "COMMITTED")
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"image-provider": 3.0})
        self.now[0] += timedelta(seconds=1)
        blocked_refresh = self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 3.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        self.assertEqual(blocked_refresh["status"], "CREDIT_BALANCE_UPDATED")
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"image-provider": 3.0})
        duplicate = self.manager.reserve(
            provider_id="image-provider", amount=2.0, request_id="credit-job", unit="USD"
        )
        self.assertEqual(duplicate["status"], "ALREADY_COMMITTED")
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"image-provider": 3.0})

    def test_credit_refresh_rejects_snapshot_older_than_local_usage(self):
        self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        stale_observation = self.now[0].isoformat()
        self.manager.reserve(
            provider_id="image-provider", amount=2.0, request_id="old-snapshot-job", unit="USD"
        )
        self.now[0] += timedelta(seconds=5)
        self.manager.commit(request_id="old-snapshot-job")
        self.now[0] += timedelta(seconds=5)
        result = self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
                "observed_at": stale_observation,
            },
        )
        self.assertEqual(result["status"], "CREDIT_SNAPSHOT_PREDATES_LOCAL_USAGE")
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"image-provider": 3.0})

    def test_credit_refresh_is_blocked_while_reservation_is_in_flight(self):
        self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        self.manager.reserve(
            provider_id="image-provider", amount=2.0, request_id="pending-credit-job", unit="USD"
        )
        result = self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 4.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        self.assertEqual(result["status"], "CREDIT_RESERVATIONS_IN_FLIGHT")
        self.assertEqual(self.manager.usable_free_credits(unit="USD"), {"image-provider": 3.0})

    def test_stale_or_unverified_balances_never_authorize_free_routing(self):
        self.manager.refresh(
            "image-provider",
            lambda _: {
                "verified": True, "free_credits_remaining": 2.0,
                "source": "official-account-api",
            },
        )
        self.now[0] += timedelta(seconds=301)
        self.assertEqual(
            self.manager.get_balance("image-provider")["status"],
            "CREDIT_BALANCE_STALE",
        )
        self.assertEqual(self.manager.usable_free_credits(), {})
        unverified = self.manager.refresh(
            "other-provider",
            lambda _: {"verified": False, "free_credits_remaining": 999.0, "source": "guess"},
        )
        self.assertEqual(unverified["status"], "UNVERIFIED_CREDIT_BALANCE")
        self.assertEqual(self.manager.usable_free_credits(), {})

    def test_invalid_credit_amount_source_and_timestamp_are_rejected(self):
        cases = [
            {"verified": True, "free_credits_remaining": float("nan"), "source": "api"},
            {"verified": True, "free_credits_remaining": 1.0, "source": ""},
            {"verified": True, "free_credits_remaining": 1.0, "source": "api", "observed_at": "not-a-time"},
            {
                "verified": True, "free_credits_remaining": 1.0, "source": "api",
                "observed_at": "2027-01-01T00:00:00+00:00",
            },
        ]
        expected = [
            "INVALID_CREDIT_AMOUNT", "INVALID_CREDIT_SOURCE",
            "INVALID_CREDIT_TIMESTAMP", "FUTURE_CREDIT_TIMESTAMP",
        ]
        for result, status in zip(cases, expected):
            with self.subTest(status=status):
                self.assertEqual(
                    self.manager.refresh("provider", lambda _: result)["status"],
                    status,
                )

    def test_probe_failures_do_not_leak_exception_details(self):
        def probe(_):
            raise RuntimeError("private token and endpoint")
        result = self.manager.refresh("provider", probe)
        self.assertEqual(result["status"], "CREDIT_PROBE_FAILED")
        self.assertNotIn("private token and endpoint", repr(result))

    def test_unknown_provider_and_invalid_probe_are_rejected(self):
        self.assertEqual(self.manager.get_balance("unknown")["status"], "CREDIT_BALANCE_UNKNOWN")
        self.assertEqual(self.manager.refresh("", lambda _: {})["status"], "INVALID_PROVIDER")
        self.assertEqual(self.manager.refresh("provider", None)["status"], "INVALID_PROBE")

    def test_invalid_configuration_and_corrupt_state_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderCreditManager(self.path, max_age_seconds=float("inf"))
        self.path.write_text(json.dumps({"providers": {"bad": {"free_credits_remaining": "many", "verified": True, "observed_at": "2026-10-11T12:00:00+00:00"}}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderCreditManager(self.path)


if __name__ == "__main__":
    unittest.main()
