import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.concurrency_manager import ProviderConcurrencyManager


def _acquire_concurrency_in_process(args):
    path, index = args
    manager = ProviderConcurrencyManager(
        path, default_provider_limit=2, global_limit=100, project_limit=100
    )
    return manager.acquire(
        provider_id="image", project_id="film", request_id=f"process-{index}"
    )


class ProviderConcurrencyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "concurrency.json"
        self.manager = ProviderConcurrencyManager(
            self.path, default_provider_limit=3, global_limit=10, project_limit=5
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_provider_limit_is_used(self):
        self.assertEqual(self.manager.get_provider_limit("image"), 3)

    def test_provider_specific_limit_persists_across_reload(self):
        result = self.manager.set_provider_limit("image", 2)
        self.assertEqual(result["status"], "LIMIT_UPDATED")
        loaded = ProviderConcurrencyManager(
            self.path, default_provider_limit=9, global_limit=10, project_limit=5
        )
        self.assertEqual(loaded.get_provider_limit("image"), 2)

    def test_invalid_limits_are_rejected(self):
        for kwargs in (
            {"default_provider_limit": 0},
            {"default_provider_limit": True},
            {"global_limit": 0},
            {"project_limit": 1.5},
            {"max_entries": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ProviderConcurrencyManager(self.path, **kwargs)
        self.assertEqual(self.manager.set_provider_limit("image", 0)["status"], "INVALID_CONCURRENCY_LIMIT")
        self.assertEqual(self.manager.set_provider_limit("bad provider", 2)["status"], "INVALID_PROVIDER_ID")

    def test_acquire_persists_reserved_lease(self):
        result = self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.assertTrue(result["acquired"])
        self.assertEqual(result["status"], "LEASE_ACQUIRED")
        loaded = ProviderConcurrencyManager(self.path)
        self.assertEqual(loaded.get("job-1")["lease"]["status"], "RESERVED")

    def test_duplicate_active_lease_is_not_acquired_twice(self):
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        duplicate = self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.assertEqual(duplicate["status"], "LEASE_ALREADY_ACTIVE")
        self.assertFalse(duplicate["acquired"])

    def test_reusing_request_id_for_other_provider_is_conflict(self):
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        result = self.manager.acquire(provider_id="video", project_id="film", request_id="job-1")
        self.assertEqual(result["status"], "REQUEST_ID_CONFLICT")
        self.assertFalse(result["acquired"])

    def test_provider_limit_rejects_excess_active_leases(self):
        self.manager.set_provider_limit("image", 2)
        self.assertTrue(self.manager.acquire(provider_id="image", project_id="film", request_id="a")["acquired"])
        self.assertTrue(self.manager.acquire(provider_id="image", project_id="film", request_id="b")["acquired"])
        blocked = self.manager.acquire(provider_id="image", project_id="film", request_id="c")
        self.assertEqual(blocked["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")
        self.assertFalse(blocked["acquired"])

    def test_project_limit_is_enforced_across_providers(self):
        manager = ProviderConcurrencyManager(self.path, default_provider_limit=10, global_limit=10, project_limit=2)
        self.assertTrue(manager.acquire(provider_id="image", project_id="film", request_id="a")["acquired"])
        self.assertTrue(manager.acquire(provider_id="video", project_id="film", request_id="b")["acquired"])
        result = manager.acquire(provider_id="voice", project_id="film", request_id="c")
        self.assertEqual(result["status"], "PROJECT_CONCURRENCY_LIMIT_REACHED")

    def test_global_limit_is_enforced_across_projects(self):
        manager = ProviderConcurrencyManager(self.path, default_provider_limit=10, global_limit=2, project_limit=10)
        self.assertTrue(manager.acquire(provider_id="image", project_id="film", request_id="a")["acquired"])
        self.assertTrue(manager.acquire(provider_id="video", project_id="music", request_id="b")["acquired"])
        result = manager.acquire(provider_id="voice", project_id="docs", request_id="c")
        self.assertEqual(result["status"], "GLOBAL_CONCURRENCY_LIMIT_REACHED")

    def test_start_transition_requires_reserved_lease(self):
        result = self.manager.mark_started(lease_id="missing")
        self.assertEqual(result["status"], "LEASE_NOT_RESERVED")
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.assertTrue(self.manager.mark_started(lease_id="job-1")["saved"])
        self.assertEqual(self.manager.mark_started(lease_id="job-1")["status"], "LEASE_NOT_RESERVED")

    def test_completed_lease_releases_capacity(self):
        self.manager.set_provider_limit("image", 1)
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.manager.mark_started(lease_id="job-1")
        self.assertTrue(self.manager.release(lease_id="job-1", outcome="completed")["saved"])
        next_lease = self.manager.acquire(provider_id="image", project_id="film", request_id="job-2")
        self.assertTrue(next_lease["acquired"])

    def test_confirmed_unstarted_lease_releases_capacity(self):
        self.manager.set_provider_limit("image", 1)
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.assertTrue(self.manager.release(lease_id="job-1", outcome="not_started")["saved"])
        self.assertTrue(self.manager.acquire(provider_id="image", project_id="film", request_id="job-2")["acquired"])

    def test_unknown_outcome_retains_capacity_until_reconciliation(self):
        self.manager.set_provider_limit("image", 1)
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.manager.mark_started(lease_id="job-1")
        unknown = self.manager.release(lease_id="job-1", outcome="unknown")
        self.assertEqual(unknown["status"], "LEASE_RETAINED_UNKNOWN")
        self.assertTrue(unknown["retained"])
        blocked = self.manager.acquire(provider_id="image", project_id="film", request_id="job-2")
        self.assertEqual(blocked["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")
        self.assertFalse(self.manager.release(lease_id="job-1", outcome="completed")["saved"])
        self.assertFalse(self.manager.reconcile(lease_id="job-1", outcome="not_running", confirmed=False)["saved"])
        reconciled = self.manager.reconcile(lease_id="job-1", outcome="not_running", confirmed=True)
        self.assertEqual(reconciled["status"], "LEASE_RECONCILED")
        self.assertTrue(self.manager.acquire(provider_id="image", project_id="film", request_id="job-2")["acquired"])

    def test_stale_started_lease_is_not_expired_automatically(self):
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.manager.mark_started(lease_id="job-1")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["leases"]["job-1"]["updated_at"] = "2000-01-01T00:00:00+00:00"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = ProviderConcurrencyManager(self.path, default_provider_limit=1, global_limit=10, project_limit=5)
        result = loaded.acquire(provider_id="image", project_id="film", request_id="job-2")
        self.assertEqual(result["status"], "PROVIDER_CONCURRENCY_LIMIT_REACHED")

    def test_reconcile_requires_ambiguous_active_lease(self):
        self.manager.acquire(provider_id="image", project_id="film", request_id="job-1")
        self.assertEqual(self.manager.reconcile(lease_id="job-1", outcome="completed", confirmed=True)["status"], "LEASE_NOT_AMBIGUOUS")
        self.assertEqual(self.manager.reconcile(lease_id="missing", outcome="completed", confirmed=True)["status"], "LEASE_NOT_FOUND")
        self.manager.mark_started(lease_id="job-1")
        self.assertEqual(self.manager.reconcile(lease_id="job-1", outcome="unknown", confirmed=True)["status"], "INVALID_RECONCILIATION_OUTCOME")

    def test_invalid_inputs_fail_closed(self):
        self.assertEqual(self.manager.acquire(provider_id="", project_id="film", request_id="job")["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(self.manager.acquire(provider_id="image", project_id="", request_id="job")["status"], "INVALID_PROJECT_ID")
        self.assertEqual(self.manager.acquire(provider_id="image", project_id="film", request_id="bad id")["status"], "INVALID_REQUEST_ID")
        self.assertEqual(self.manager.release(lease_id="job", outcome="bad")["status"], "INVALID_LEASE_OUTCOME")
        self.assertEqual(self.manager.release(lease_id="job", outcome=[])["status"], "INVALID_LEASE_OUTCOME")
        self.assertEqual(self.manager.get("bad id")["status"], "INVALID_LEASE_ID")

    def test_capacity_fails_closed_without_evicting_old_leases(self):
        manager = ProviderConcurrencyManager(self.path, max_entries=1, global_limit=10, project_limit=10)
        manager.acquire(provider_id="image", project_id="film", request_id="first")
        result = manager.acquire(provider_id="image", project_id="film", request_id="second")
        self.assertEqual(result["status"], "CONCURRENCY_LEDGER_CAPACITY_REACHED")
        self.assertEqual(manager.get("first")["status"], "FOUND")
        self.assertEqual(manager.get("second")["status"], "NOT_FOUND")

    def test_corrupt_json_and_invalid_persisted_state_are_rejected(self):
        self.path.write_text("{broken-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderConcurrencyManager(self.path)
        self.path.write_text(json.dumps({
            "version": "1.0", "provider_limits": {},
            "leases": {"job": {"provider_id": "image", "project_id": "film", "status": [], "created_at": "x", "updated_at": "x"}},
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderConcurrencyManager(self.path)

    def test_concurrent_threads_never_exceed_provider_limit(self):
        manager = ProviderConcurrencyManager(self.path, default_provider_limit=3, global_limit=100, project_limit=100)
        def acquire(index):
            return manager.acquire(provider_id="image", project_id="film", request_id=f"thread-{index}")
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(acquire, range(40)))
        self.assertEqual(sum(result.get("acquired") is True for result in results), 3)
        self.assertEqual(sum(result["status"] == "PROVIDER_CONCURRENCY_LIMIT_REACHED" for result in results), 37)

    def test_concurrent_processes_never_exceed_provider_limit(self):
        args = [(str(self.path), index) for index in range(20)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_acquire_concurrency_in_process, args))
        self.assertEqual(sum(result.get("acquired") is True for result in results), 2)
        self.assertEqual(sum(result["status"] == "PROVIDER_CONCURRENCY_LIMIT_REACHED" for result in results), 18)

    def test_summary_counts_active_and_terminal_leases(self):
        self.manager.acquire(provider_id="image", project_id="film", request_id="active")
        self.manager.acquire(provider_id="video", project_id="film", request_id="done")
        self.manager.mark_started(lease_id="done")
        self.manager.release(lease_id="done", outcome="completed")
        summary = self.manager.summary()
        self.assertEqual(summary["RESERVED"], 1)
        self.assertEqual(summary["COMPLETED"], 1)


if __name__ == "__main__":
    unittest.main()
