"""Durable, fail-closed tenant request admission based on GCRA.

Only authenticated tenant/role identifiers are accepted. A single SQLite
transaction serializes admissions across local worker processes. Callers
must authenticate requests before invoking the limiter.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}\\Z")
_ROLES = frozenset(("client", "producer"))
_MICROSECOND = 1_000_000


class RateLimitExceeded(Exception):
    """An authenticated request exceeded the server's rate policy."""

    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__("Rate limit exceeded")


class DurableTenantRateLimiter:
    """Multi-process-safe GCRA limiter with separate tenant/role buckets.

    Tenant names are hashed and credentials, signatures and request bodies
    are never persisted. Database errors fail closed rather than bypassing
    admission.
    """

    def __init__(self, path: str | os.PathLike[str], *, max_events: int = 600,
                 window_seconds: int = 60):
        for value, minimum, maximum, label in (
            (max_events, 1, 100_000, "max_events"),
            (window_seconds, 1, 3_600, "window_seconds"),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"Unsafe {label}")
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.interval_us = max(1, (window_seconds * _MICROSECOND + max_events - 1) // max_events)
        self.burst_tolerance_us = (max_events - 1) * self.interval_us
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("Untrusted admission store")
        if not self.path.parent.is_dir():
            raise ValueError("Admission store directory must exist")
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("CREATE TABLE IF NOT EXISTS quota (scope_hash TEXT PRIMARY KEY, tat_us INTEGER NOT NULL CHECK(tat_us >= 0))")
            schema = db.execute("PRAGMA table_info(quota)").fetchall()
            if [(row[1], row[2]) for row in schema] != [("scope_hash", "TEXT"), ("tat_us", "INTEGER")]:
                raise ValueError("Unexpected admission store schema")
            db.commit()
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def check(self, tenant: str, role: str, now: int) -> None:
        """Consume one authenticated slot or reject with retry seconds."""
        if (type(tenant) is not str or _SCOPE.fullmatch(tenant) is None or
                role not in _ROLES or type(now) is not int or
                not 0 <= now <= 9_000_000_000):
            raise ValueError("Invalid admission request")
        scope_hash = hashlib.sha256(b"ai-creator-job-quota-v1\x00" +
                                    tenant.encode("ascii") + b"\x00" + role.encode("ascii")).hexdigest()
        now_us = now * _MICROSECOND
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA busy_timeout=10000")
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT tat_us FROM quota WHERE scope_hash=?", (scope_hash,)).fetchone()
                tat_us = row[0] if row else 0
                if type(tat_us) is not int or tat_us < 0:
                    raise ValueError("Invalid admission state")
                permitted_at = tat_us - self.burst_tolerance_us
                if now_us < permitted_at:
                    delay = max(1, (permitted_at - now_us + _MICROSECOND - 1) // _MICROSECOND)
                    db.rollback()
                    raise RateLimitExceeded(delay)
                new_tat = max(tat_us, now_us) + self.interval_us
                db.execute("INSERT INTO quota(scope_hash,tat_us) VALUES(?,?) "
                           "ON CONFLICT(scope_hash) DO UPDATE SET tat_us=excluded.tat_us",
                           (scope_hash, new_tat))
                if (now & 127) == 0:
                    db.execute("DELETE FROM quota WHERE tat_us < ?", (now_us - self.window_seconds * _MICROSECOND * 2,))
                db.commit()
            except RateLimitExceeded:
                raise
            except BaseException:
                db.rollback()
                raise
