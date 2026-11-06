import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_concurrency_manager import ProviderConcurrencyManager


class ProviderConcurrencyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "provider-concurrency.json"
        self.now = 1000.0
        self.manager = ProviderConcurrencyManager(
            self.path, provider_limits={"image": 1, "video": 2},
            default_limit=3, lease_seconds=10, heartbeat_interval=1.0,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.temp.cleanup()

    def acquire(self, provider="image", request="request-1", project="film", tenant="tenant-a", **kwargs):
        return self.manager.acquire(
            provider_id=provider, request_id=request, project_id=project,
            tenant_id=tenant, **kwargs,
        )

    def test_provider_specific_limit_blocks_excess_worker_and_release_frees_slot(self):
        first = self.acquire()
        self.assertTrue(first["acquired"])
        blocked = self.acquire(request="request-2")
        self.assertEqual(blocked["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")
        self.assertEqual(blocked["active"], 1)
        self.assertEqual(blocked["limit"], 1)
        released = self.manager.release(first["lease_token"])
        self.assertTrue(released["released"])
        next_lease = self.acquire(request="request-2")
        self.assertTrue(next_lease["acquired"])

    def test_limits_are_isolated_by_provider(self):
        image = self.acquire(provider="image", request="image-1")
        video1 = self.acquire(provider="video", request="video-1")
        video2 = self.acquire(provider="video", request="video-2")
        video3 = self.acquire(provider="video", request="video-3")
        self.assertTrue(image["acquired"])
        self.assertTrue(video1["acquired"])
        self.assertTrue(video2["acquired"])
        self.assertEqual(video3["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")

    def test_duplicate_active_request_is_rejected_even_below_limit(self):
        self.manager = ProviderConcurrencyManager(
            self.path, default_limit=5, lease_seconds=10, clock=lambda: self.now,
        )
        first = self.acquire()
        duplicate = self.acquire()
        self.assertTrue(first["acquired"])
        self.assertEqual(duplicate["status"], "DUPLICATE_CONCURRENCY_REQUEST")

    def test_lease_renewal_and_release_are_idempotent(self):
        lease = self.acquire()
        renewed = self.manager.renew(lease["lease_token"])
        self.assertTrue(renewed["renewed"])
        self.assertGreater(renewed["expires_at"], lease["expires_at"])
        released = self.manager.release(lease["lease_token"])
        repeated = self.manager.release(lease["lease_token"])
        self.assertTrue(released["released"])
        self.assertEqual(repeated["status"], "ALREADY_RELEASED")

    def test_expired_leases_are_purged_and_capacity_reused(self):
        lease = self.acquire()
        self.now += 11
        next_lease = self.acquire(request="request-2")
        self.assertTrue(next_lease["acquired"])
        self.assertEqual(self.manager.get_status("image")["active"], 1)
        self.assertEqual(
            self.manager.renew(lease["lease_token"])["status"],
            "CONCURRENCY_LEASE_NOT_FOUND",
        )

    def test_concurrent_acquisitions_respect_single_provider_slot(self):
        other = ProviderConcurrencyManager(
            self.path, provider_limits={"image": 1}, lease_seconds=10,
            heartbeat_interval=1.0, clock=lambda: self.now,
        )
        managers = [self.manager, other]

        def claim(index):
            return managers[index % 2].acquire(
                provider_id="image", request_id=f"parallel-{index}",
                project_id="film", tenant_id="tenant-a",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(sum(item.get("acquired") is True for item in results), 1)
        self.assertEqual(
            sum(item.get("status") == "PROVIDER_CONCURRENCY_LIMIT_REACHED" for item in results),
            15,
        )

    def test_heartbeat_keeps_long_running_provider_lease_alive(self):
        manager = ProviderConcurrencyManager(
            self.path, provider_limits={"image": 1}, lease_seconds=3,
            heartbeat_interval=0.2,
        )
        lease = manager.acquire(
            provider_id="image", request_id="long-running",
            project_id="film", tenant_id="tenant-a",
        )
        self.assertTrue(lease["acquired"])
        with manager.maintain_lease(lease["lease_token"]) as lost:
            time.sleep(3.3)
            self.assertFalse(lost.is_set())
            self.assertEqual(manager.get_status("image")["active"], 1)
        self.assertTrue(manager.release(lease["lease_token"])["released"])

    def test_malformed_state_and_invalid_configuration_fail_closed(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        result = self.acquire()
        self.assertEqual(result["status"], "CONCURRENCY_STATE_INVALID")
        with self.assertRaises(ValueError):
            ProviderConcurrencyManager(
                self.path.parent / "bad.json", provider_limits={"image": 0},
            )

    def test_capacity_and_identifier_validation(self):
        small = ProviderConcurrencyManager(
            self.path.parent / "small.json", default_limit=1,
            max_leases=1, lease_seconds=10, clock=lambda: self.now,
        )
        first = small.acquire(
            provider_id="image", request_id="one", project_id="film",
        )
        self.assertTrue(first["acquired"])
        invalid = small.acquire(
            provider_id="image", request_id="bad request", project_id="film",
        )
        self.assertEqual(invalid["status"], "INVALID_CONCURRENCY_IDENTITY")
        # The existing lease occupies the only slot before storage capacity is reached.
        blocked = small.acquire(
            provider_id="video", request_id="two", project_id="film",
        )
        self.assertEqual(blocked["status"], "CONCURRENCY_CAPACITY_REACHED")


if __name__ == "__main__":
    unittest.main()
