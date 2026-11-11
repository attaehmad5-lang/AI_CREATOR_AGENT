"""Offline durable GCRA admission regressions (no live providers)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from agents.jobs.request_quota import DurableTenantRateLimiter, RateLimitExceeded


class DurableRequestQuotaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "quota.sqlite3"
        self.limit = DurableTenantRateLimiter(self.path, max_events=3, window_seconds=6)
        self.now = 2000

    def test_burst_allowance_and_rejection(self):
        for _ in range(3):
            self.limit.check("alpha", "client", self.now)
        with self.assertRaises(RateLimitExceeded) as err:
            self.limit.check("alpha", "client", self.now)
        self.assertEqual(err.exception.retry_after, 2)

    def test_interval_restore_after_burst(self):
        for _ in range(3):
            self.limit.check("alpha", "client", self.now)
        self.limit.check("alpha", "client", self.now + 2)
        with self.assertRaises(RateLimitExceeded):
            self.limit.check("alpha", "client", self.now + 2)

    def test_rate_limiter_persists_on_restart(self):
        for _ in range(3):
            self.limit.check("alpha", "client", self.now)
        restarted = DurableTenantRateLimiter(self.path, max_events=3, window_seconds=6)
        with self.assertRaises(RateLimitExceeded):
            restarted.check("alpha", "client", self.now)

    def test_tenant_and_role_scopes_separate(self):
        for _ in range(3):
            self.limit.check("alpha", "client", self.now)
        self.limit.check("alpha", "producer", self.now)
        self.limit.check("beta", "client", self.now)

    def test_parallel_admission_never_overbooks(self):
        def attempt(_):
            try:
                self.limit.check("alpha", "client", self.now)
                return True
            except RateLimitExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(attempt, range(25)))
        self.assertEqual(sum(result), 3)

    def test_rejection_does_not_consume_another_slot(self):
        for _ in range(3):
            self.limit.check("alpha", "client", self.now)
        for _ in range(4):
            with self.assertRaises(RateLimitExceeded):
                self.limit.check("alpha", "client", self.now)
        self.limit.check("alpha", "client", self.now + 2)

    def test_invalid_configurations_fail_closed(self):
        for kwargs in ({"max_events": 0}, {"window_seconds": -1},
                       {"max_events": True}, {"window_seconds": 3601}):
            with self.assertRaises(ValueError):
                DurableTenantRateLimiter(self.path, **kwargs)

    def test_bad_identifier_or_role_rejected(self):
        for tenant, role, now in (("../a", "client", self.now),
                                  ("a", "admin", self.now),
                                  ("a", "client", -1),
                                  ("a", "client", True)):
            with self.assertRaises(ValueError):
                self.limit.check(tenant, role, now)

    def test_plaintext_tenant_not_persisted(self):
        self.limit.check("sensitiveTenant", "client", self.now)
        self.assertNotIn(b"sensitiveTenant", self.path.read_bytes())

    def test_existing_database_schema_tampering_fails_closed(self):
        with closing(sqlite3.connect(self.path)) as conn, conn as db:
            db.execute("ALTER TABLE quota ADD COLUMN unexpected TEXT")
        with self.assertRaises(ValueError):
            DurableTenantRateLimiter(self.path)

    def test_symlink_storage_rejected(self):
        self.path.unlink()
        actual = self.path.parent / "other.sqlite3"
        actual.write_text("untouched")
        try:
            self.path.symlink_to(actual)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink unsupported")
        with self.assertRaises(ValueError):
            DurableTenantRateLimiter(self.path)
        self.assertEqual(actual.read_text(), "untouched")

    def test_unreadable_schema_fails_closed(self):
        self.path.write_bytes(b"corrupt SQLite")
        with self.assertRaises(sqlite3.DatabaseError):
            DurableTenantRateLimiter(self.path)


if __name__ == "__main__":
    unittest.main()
