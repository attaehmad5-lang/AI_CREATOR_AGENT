"""Deployment adapters and durable execution helpers for privacy lifecycles.

This module intentionally does not invent cloud credentials or provider APIs.
It supplies a hardened HTTPS adapter contract, a persistent idempotency wrapper
for storage/provider adapters, and an admin-gated retention job callable by an
external scheduler (cron, systemd, Kubernetes CronJob, or a cloud scheduler).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}\Z")
_DELETE_SUCCESS = frozenset({"DELETED", "ALREADY_ABSENT"})
_PURGE_SUCCESS = frozenset({"PURGED", "ALREADY_EMPTY"})
_FAILURE = {"confirmed": False, "status": "FAILED", "deleted_count": 0}


class PrivacyAdapterConfigurationError(ValueError):
    """Raised when a privacy adapter has an unsafe or incomplete configuration."""


def _valid_reference(value: Any) -> bool:
    return isinstance(value, str) and _REFERENCE_RE.fullmatch(value) is not None


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_RE.fullmatch(value) is not None


def _valid_subject_ref(value: Any) -> bool:
    return _valid_reference(value) and any(char.isalpha() for char in value)


def _normalise_adapter_result(value: Any, *, operation: str) -> dict[str, Any]:
    allowed = _DELETE_SUCCESS if operation == "delete" else _PURGE_SUCCESS
    if not isinstance(value, Mapping):
        return dict(_FAILURE)
    status = value.get("status")
    confirmed = value.get("confirmed")
    count = value.get("deleted_count")
    if (
        not isinstance(status, str)
        or status not in allowed | {"PENDING", "FAILED"}
        or not isinstance(confirmed, bool)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= 1_000_000_000_000
    ):
        return dict(_FAILURE)
    if confirmed != (status in allowed) or (not confirmed and count != 0):
        return dict(_FAILURE)
    return {"confirmed": confirmed, "status": status, "deleted_count": count}


class PersistentPrivacyAdapter:
    """Wrap a deletion adapter with durable, privacy-preserving request deduplication.

    The SQLite ledger stores only HMAC-SHA-256 digests of request/scope
    identifiers, status, counters and timestamps. A deployment-managed stable
    32-byte digest key is required and is resolved using an adapter-specific
    context. It never stores subject references, tenant
    IDs, project IDs, credentials, or raw adapter responses. The wrapped delete
    or purge operation must still be idempotent: after a process crash, an
    expired RUNNING/PENDING lease is retried with the same request ID.

    Use a durable local volume for ``ledger_path``. For horizontally scaled
    deployments, all workers for this adapter must share a storage system whose
    SQLite locking semantics are reliable; otherwise use a deployment-specific
    distributed ledger implementing the same claim/complete protocol.
    """

    def __init__(
        self,
        adapter: Any,
        *,
        ledger_path: str | os.PathLike[str],
        adapter_id: str,
        digest_key_provider: Callable[[str], bytes],
        lease_seconds: int = 300,
        max_requests: int = 1_000_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not callable(getattr(adapter, "delete_subject_data", None)) or not callable(
            getattr(adapter, "purge_expired_data", None)
        ):
            raise PrivacyAdapterConfigurationError("wrapped adapter lacks the privacy lifecycle contract")
        if not isinstance(adapter_id, str) or _COMPONENT_RE.fullmatch(adapter_id) is None:
            raise PrivacyAdapterConfigurationError("adapter_id is invalid")
        if not callable(digest_key_provider):
            raise PrivacyAdapterConfigurationError("digest_key_provider must be a deployment-managed callable")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86_400:
            raise PrivacyAdapterConfigurationError("lease_seconds is outside safe limits")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or not 1 <= max_requests <= 10_000_000:
            raise PrivacyAdapterConfigurationError("max_requests is outside safe limits")
        if clock is not None and not callable(clock):
            raise PrivacyAdapterConfigurationError("clock must be callable")
        self.adapter = adapter
        self.adapter_id = adapter_id
        self.digest_key_provider = digest_key_provider
        self.digest_context = f"privacy-idempotency:{adapter_id}"
        self._digest_key_fingerprint = hashlib.sha256(self._digest_key()).hexdigest()
        self.lease_seconds = lease_seconds
        self.max_requests = max_requests
        self.clock = clock or time.time
        self.ledger_path = Path(ledger_path)
        self._digest_key()  # Fail at startup, not on the first privacy operation.
        if self.ledger_path.exists() and self.ledger_path.is_symlink():
            raise PrivacyAdapterConfigurationError("privacy request ledger symlinks are not accepted")
        if self.ledger_path.exists() and not self.ledger_path.is_file():
            raise PrivacyAdapterConfigurationError("privacy request ledger path must be a file")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise_ledger()

    def _digest_key(self) -> bytes:
        try:
            key = self.digest_key_provider(self.digest_context)
        except Exception as exc:
            raise PrivacyAdapterConfigurationError("privacy digest key is unavailable") from exc
        if not isinstance(key, bytes) or len(key) != 32:
            raise PrivacyAdapterConfigurationError("privacy digest key must be exactly 32 bytes")
        fingerprint = hashlib.sha256(key).hexdigest()
        expected = getattr(self, "_digest_key_fingerprint", fingerprint)
        if fingerprint != expected:
            raise PrivacyAdapterConfigurationError("privacy digest key changed during operation")
        return key

    @staticmethod
    def _digest(value: Any, key: bytes) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        if self.ledger_path.is_symlink():
            raise OSError("privacy ledger symlink detected")
        connection = sqlite3.connect(str(self.ledger_path), timeout=5.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialise_ledger(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS privacy_request_ledger_metadata (
                    adapter_id TEXT PRIMARY KEY,
                    digest_key_fingerprint TEXT NOT NULL
                )"""
            )
            fingerprint = hashlib.sha256(self._digest_key()).hexdigest()
            existing = connection.execute(
                "SELECT digest_key_fingerprint FROM privacy_request_ledger_metadata WHERE adapter_id=?",
                (self.adapter_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO privacy_request_ledger_metadata(adapter_id, digest_key_fingerprint) VALUES (?, ?)",
                    (self.adapter_id, fingerprint),
                )
            elif existing[0] != fingerprint:
                raise PrivacyAdapterConfigurationError(
                    "privacy digest key changed; preserve the existing idempotency key or migrate the ledger"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS privacy_request_ledger (
                    adapter_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('RUNNING','PENDING','FAILED','COMPLETE')),
                    result_status TEXT,
                    deleted_count INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(adapter_id, request_digest)
                )"""
            )
            if os.name == "posix":
                os.chmod(self.ledger_path, 0o600)
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _claim(self, request_digest: str, operation: str, scope_digest: str) -> dict[str, Any] | None:
        connection = None
        try:
            now = float(self.clock())
            if not math.isfinite(now):
                return dict(_FAILURE)
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation, scope_digest, state, result_status, deleted_count, updated_at, attempts "
                "FROM privacy_request_ledger WHERE adapter_id=? AND request_digest=?",
                (self.adapter_id, request_digest),
            ).fetchone()
            if row is not None:
                old_operation, old_scope, state, result_status, count, updated_at, attempts = row
                if old_operation != operation or old_scope != scope_digest:
                    connection.execute("COMMIT")
                    return dict(_FAILURE)
                if state == "COMPLETE":
                    allowed = _DELETE_SUCCESS if operation == "delete" else _PURGE_SUCCESS
                    if result_status not in allowed or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000_000_000:
                        connection.execute("COMMIT")
                        return dict(_FAILURE)
                    connection.execute("COMMIT")
                    return {
                        "confirmed": True,
                        "status": result_status,
                        "deleted_count": count,
                        "replayed": True,
                    }
                if state in {"RUNNING", "PENDING"} and now - float(updated_at) < self.lease_seconds:
                    connection.execute("COMMIT")
                    return {"confirmed": False, "status": "PENDING", "deleted_count": 0}
                connection.execute(
                    "UPDATE privacy_request_ledger SET state='RUNNING', attempts=?, updated_at=?, "
                    "result_status=NULL, deleted_count=0 WHERE adapter_id=? AND request_digest=?",
                    (int(attempts) + 1, now, self.adapter_id, request_digest),
                )
            else:
                total = connection.execute(
                    "SELECT COUNT(*) FROM privacy_request_ledger WHERE adapter_id=?",
                    (self.adapter_id,),
                ).fetchone()[0]
                if total >= self.max_requests:
                    connection.execute("COMMIT")
                    return dict(_FAILURE)
                connection.execute(
                    "INSERT INTO privacy_request_ledger "
                    "(adapter_id, request_digest, operation, scope_digest, state, updated_at) "
                    "VALUES (?, ?, ?, ?, 'RUNNING', ?)",
                    (self.adapter_id, request_digest, operation, scope_digest, now),
                )
            connection.execute("COMMIT")
            claimed_attempt = int(attempts) + 1 if row is not None else 1
            return {"__claim_attempt": claimed_attempt}
        except Exception:
            if connection is not None:
                try:
                    connection.execute("ROLLBACK")
                except Exception:
                    pass
            return dict(_FAILURE)
        finally:
            if connection is not None:
                connection.close()

    def _finish(self, request_digest: str, result: dict[str, Any], *, attempt: int) -> bool:
        try:
            now = float(self.clock())
        except Exception:
            return False
        if not math.isfinite(now):
            return False
        state = "COMPLETE" if result["confirmed"] else "PENDING" if result["status"] == "PENDING" else "FAILED"
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE privacy_request_ledger SET state=?, result_status=?, deleted_count=?, updated_at=? "
                "WHERE adapter_id=? AND request_digest=? AND attempts=? AND state='RUNNING'",
                (state, result["status"], result["deleted_count"], now, self.adapter_id, request_digest, attempt),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            return False
        finally:
            if connection is not None:
                connection.close()

    def _run(self, *, operation: str, request_id: str, scope: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        if not _valid_request_id(request_id):
            return dict(_FAILURE)
        try:
            digest_key = self._digest_key()
            request_digest = hmac.new(digest_key, request_id.encode("ascii"), hashlib.sha256).hexdigest()
            scope_digest = self._digest({"operation": operation, **scope}, digest_key)
        except (TypeError, ValueError, UnicodeEncodeError, PrivacyAdapterConfigurationError):
            return dict(_FAILURE)
        claim = self._claim(request_digest, operation, scope_digest)
        if claim is None:
            return dict(_FAILURE)
        if "__claim_attempt" not in claim:
            return claim
        attempt = claim["__claim_attempt"]
        try:
            raw = getattr(self.adapter, "delete_subject_data" if operation == "delete" else "purge_expired_data")(**kwargs)
        except Exception:
            raw = dict(_FAILURE)
        result = _normalise_adapter_result(raw, operation=operation)
        if not self._finish(request_digest, result, attempt=attempt):
            # A failed durable completion write cannot be reported as success.
            return {"confirmed": False, "status": "PENDING", "deleted_count": 0}
        return result

    def delete_subject_data(self, *, subject_ref: str, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if (
            not _valid_subject_ref(subject_ref)
            or not _valid_reference(project_id)
            or (tenant_id is not None and not _valid_reference(tenant_id))
        ):
            return dict(_FAILURE)
        return self._run(
            operation="delete",
            request_id=request_id,
            scope={"subject_ref": subject_ref, "project_id": project_id, "tenant_id": tenant_id},
            kwargs={"subject_ref": subject_ref, "project_id": project_id, "tenant_id": tenant_id, "request_id": request_id},
        )

    def purge_expired_data(self, *, cutoff: datetime, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if (
            not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
            or not _valid_reference(project_id)
            or (tenant_id is not None and not _valid_reference(tenant_id))
        ):
            return dict(_FAILURE)
        cutoff_utc = cutoff.astimezone(timezone.utc).isoformat()
        return self._run(
            operation="purge",
            request_id=request_id,
            scope={"cutoff": cutoff_utc, "project_id": project_id, "tenant_id": tenant_id},
            kwargs={"cutoff": cutoff, "project_id": project_id, "tenant_id": tenant_id, "request_id": request_id},
        )

    def prune_completed_before(self, cutoff: datetime) -> dict[str, Any]:
        """Prune old completed/failed ledger rows; pending work is never pruned."""
        if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            return {"status": "INVALID_LEDGER_PRUNE_CUTOFF", "pruned": False, "pruned_count": 0}
        threshold = cutoff.astimezone(timezone.utc).timestamp()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM privacy_request_ledger WHERE adapter_id=? AND state IN ('COMPLETE','FAILED') AND updated_at<?",
                (self.adapter_id, threshold),
            )
            count = int(cursor.rowcount)
            connection.execute("COMMIT")
            return {"status": "PRIVACY_LEDGER_PRUNED", "pruned": True, "pruned_count": count}
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            return {"status": "PRIVACY_LEDGER_PRUNE_FAILED", "pruned": False, "pruned_count": 0}
        finally:
            connection.close()


class SQLitePrivacyDataAdapter:
    """Transactional SQLite database adapter for privacy data rows.

    The data table must contain ``subject_ref``, ``project_id``, ``tenant_id``
    and ``expires_at`` columns. Store ``expires_at`` as canonical UTC ISO-8601
    text so the retention comparison is deterministic. A deployment-managed
    stable 32-byte key HMAC-protects request and scope digests. Deletion and its
    idempotency receipt commit in the same SQLite transaction; a crash cannot
    commit one without the other. This adapter handles rows in this database,
    not external object keys referenced by those rows.
    """

    _LEDGER_TABLE = "aca_privacy_request_ledger"

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        table: str,
        digest_key_provider: Callable[[str], bytes],
        adapter_id: str = "sqlite-privacy",
        max_deleted_rows: int = 1_000_000,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not isinstance(table, str) or _COMPONENT_RE.fullmatch(table) is None:
            raise PrivacyAdapterConfigurationError("SQLite table name is invalid")
        if not isinstance(adapter_id, str) or _COMPONENT_RE.fullmatch(adapter_id) is None:
            raise PrivacyAdapterConfigurationError("adapter_id is invalid")
        if not callable(digest_key_provider):
            raise PrivacyAdapterConfigurationError("digest_key_provider must be a deployment-managed callable")
        if isinstance(max_deleted_rows, bool) or not isinstance(max_deleted_rows, int) or not 1 <= max_deleted_rows <= 10_000_000:
            raise PrivacyAdapterConfigurationError("max_deleted_rows is outside safe limits")
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or not 100 <= busy_timeout_ms <= 60_000:
            raise PrivacyAdapterConfigurationError("busy_timeout_ms is outside safe limits")
        self.database_path = Path(database_path)
        if self.database_path.exists() and self.database_path.is_symlink():
            raise PrivacyAdapterConfigurationError("SQLite privacy database symlinks are not accepted")
        if self.database_path.exists() and not self.database_path.is_file():
            raise PrivacyAdapterConfigurationError("SQLite privacy database path must be a file")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.table = table
        self.adapter_id = adapter_id
        self.digest_key_provider = digest_key_provider
        self.digest_context = f"privacy-db-idempotency:{adapter_id}"
        self.max_deleted_rows = max_deleted_rows
        self.busy_timeout_ms = busy_timeout_ms
        self._quoted_table = '"' + table + '"'
        try:
            digest_key = self.digest_key_provider(self.digest_context)
        except Exception as exc:
            raise PrivacyAdapterConfigurationError("privacy database digest key is unavailable") from exc
        if not isinstance(digest_key, bytes) or len(digest_key) != 32:
            raise PrivacyAdapterConfigurationError("privacy database digest key must be exactly 32 bytes")
        self._digest_key_fingerprint = hashlib.sha256(digest_key).hexdigest()
        self._initialise()

    def _current_digest_key(self) -> bytes:
        try:
            key = self.digest_key_provider(self.digest_context)
        except Exception as exc:
            raise PrivacyAdapterConfigurationError("privacy database digest key is unavailable") from exc
        if not isinstance(key, bytes) or len(key) != 32:
            raise PrivacyAdapterConfigurationError("privacy database digest key must be exactly 32 bytes")
        if hashlib.sha256(key).hexdigest() != self._digest_key_fingerprint:
            raise PrivacyAdapterConfigurationError("privacy database digest key changed during operation")
        return key

    def _connect(self) -> sqlite3.Connection:
        if self.database_path.is_symlink():
            raise OSError("SQLite privacy database symlink detected")
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({self._quoted_table})").fetchall()
            }
            required = {"subject_ref", "project_id", "tenant_id", "expires_at"}
            if not required.issubset(columns):
                raise PrivacyAdapterConfigurationError("SQLite data table lacks required privacy columns")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS aca_privacy_adapter_metadata (
                    adapter_id TEXT PRIMARY KEY,
                    digest_key_fingerprint TEXT NOT NULL
                )"""
            )
            digest_key = self.digest_key_provider(self.digest_context)
            if not isinstance(digest_key, bytes) or len(digest_key) != 32:
                raise PrivacyAdapterConfigurationError("privacy database digest key must be exactly 32 bytes")
            fingerprint = hashlib.sha256(digest_key).hexdigest()
            if fingerprint != self._digest_key_fingerprint:
                raise PrivacyAdapterConfigurationError("privacy database digest key changed during startup")
            existing = connection.execute(
                "SELECT digest_key_fingerprint FROM aca_privacy_adapter_metadata WHERE adapter_id=?",
                (self.adapter_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO aca_privacy_adapter_metadata(adapter_id, digest_key_fingerprint) VALUES (?, ?)",
                    (self.adapter_id, fingerprint),
                )
            elif existing[0] != fingerprint:
                raise PrivacyAdapterConfigurationError(
                    "privacy database digest key changed; migrate its idempotency ledger before rotation"
                )
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {self._LEDGER_TABLE} (
                    adapter_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('COMPLETE','FAILED')),
                    result_status TEXT NOT NULL,
                    deleted_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(adapter_id, request_digest)
                )"""
            )
            # This may be an application-owned database shared with other
            # processes; storage permissions and volume encryption belong to
            # deployment policy rather than this adapter mutating the DB mode.
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _run(self, *, operation: str, request_id: str, scope: dict[str, Any], where_sql: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        if not _valid_request_id(request_id):
            return dict(_FAILURE)
        try:
            digest_key = self._current_digest_key()
            request_digest = hmac.new(digest_key, request_id.encode("ascii"), hashlib.sha256).hexdigest()
            scope_payload = json.dumps({"operation": operation, **scope}, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
            scope_digest = hmac.new(digest_key, scope_payload, hashlib.sha256).hexdigest()
        except (TypeError, ValueError, UnicodeEncodeError):
            return dict(_FAILURE)
        except Exception:
            return dict(_FAILURE)
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                f"SELECT operation, scope_digest, state, result_status, deleted_count FROM {self._LEDGER_TABLE} WHERE adapter_id=? AND request_digest=?",
                (self.adapter_id, request_digest),
            ).fetchone()
            if prior is not None:
                old_operation, old_scope, state, status, count = prior
                if old_operation != operation or old_scope != scope_digest:
                    connection.execute("COMMIT")
                    return dict(_FAILURE)
                if state == "COMPLETE":
                    connection.execute("COMMIT")
                    return {"confirmed": True, "status": status, "deleted_count": count, "replayed": True}
                # Failed requests are safe to retry; their prior transaction did not delete rows.
                connection.execute(
                    f"DELETE FROM {self._LEDGER_TABLE} WHERE adapter_id=? AND request_digest=?",
                    (self.adapter_id, request_digest),
                )
            match = connection.execute(
                f"SELECT COUNT(*) FROM {self._quoted_table} WHERE {where_sql}", parameters
            ).fetchone()[0]
            if isinstance(match, bool) or not isinstance(match, int) or match > self.max_deleted_rows:
                connection.execute(
                    f"INSERT OR REPLACE INTO {self._LEDGER_TABLE} VALUES (?, ?, ?, ?, 'FAILED', 'FAILED', 0, ?)",
                    (self.adapter_id, request_digest, operation, scope_digest, time.time()),
                )
                connection.execute("COMMIT")
                return dict(_FAILURE)
            deleted = connection.execute(
                f"DELETE FROM {self._quoted_table} WHERE {where_sql}", parameters
            ).rowcount
            if deleted < 0 or deleted > self.max_deleted_rows:
                raise RuntimeError("invalid SQLite row count")
            status = (
                ("DELETED" if deleted else "ALREADY_ABSENT")
                if operation == "delete"
                else ("PURGED" if deleted else "ALREADY_EMPTY")
            )
            connection.execute(
                f"INSERT OR REPLACE INTO {self._LEDGER_TABLE} VALUES (?, ?, ?, ?, 'COMPLETE', ?, ?, ?)",
                (self.adapter_id, request_digest, operation, scope_digest, status, deleted, time.time()),
            )
            connection.execute("COMMIT")
            return {"confirmed": True, "status": status, "deleted_count": deleted}
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            return dict(_FAILURE)
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _scope_valid(project_id: Any, tenant_id: Any) -> bool:
        return (
            _valid_reference(project_id)
            and (tenant_id is None or _valid_reference(tenant_id))
        )

    def delete_subject_data(self, *, subject_ref: str, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if not _valid_subject_ref(subject_ref) or not self._scope_valid(project_id, tenant_id):
            return dict(_FAILURE)
        if tenant_id is None:
            where = "subject_ref=? AND project_id=? AND tenant_id IS NULL"
            parameters = (subject_ref, project_id)
        else:
            where = "subject_ref=? AND project_id=? AND tenant_id=?"
            parameters = (subject_ref, project_id, tenant_id)
        return self._run(
            operation="delete",
            request_id=request_id,
            scope={"subject_ref": subject_ref, "project_id": project_id, "tenant_id": tenant_id},
            where_sql=where,
            parameters=parameters,
        )

    def purge_expired_data(self, *, cutoff: datetime, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if (
            not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
            or not self._scope_valid(project_id, tenant_id)
        ):
            return dict(_FAILURE)
        cutoff_utc = cutoff.astimezone(timezone.utc).isoformat(timespec="microseconds")
        if tenant_id is None:
            where = "project_id=? AND tenant_id IS NULL AND expires_at IS NOT NULL AND expires_at<>'' AND expires_at<=?"
            parameters = (project_id, cutoff_utc)
        else:
            where = "project_id=? AND tenant_id=? AND expires_at IS NOT NULL AND expires_at<>'' AND expires_at<=?"
            parameters = (project_id, tenant_id, cutoff_utc)
        return self._run(
            operation="purge",
            request_id=request_id,
            scope={"cutoff": cutoff_utc, "project_id": project_id, "tenant_id": tenant_id},
            where_sql=where,
            parameters=parameters,
        )


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class HTTPSPrivacyProviderAdapter:
    """Bridge to a provider's documented privacy-deletion API over verified HTTPS.

    Endpoints and authorization are deployment supplied. Requests are bounded,
    redirects are rejected, TLS verification remains enabled, and the stable
    request ID is sent as an Idempotency-Key. The response contract is strict:
    JSON {confirmed, status, deleted_count}; raw provider text is never returned.
    """

    def __init__(
        self,
        *,
        delete_url: str,
        purge_url: str,
        allowed_hosts: Sequence[str],
        authorization_header: Callable[[], str],
        timeout: float = 5.0,
        max_response_bytes: int = 65_536,
        opener: Any = None,
    ) -> None:
        if not callable(authorization_header):
            raise PrivacyAdapterConfigurationError("authorization_header must be callable")
        if not isinstance(allowed_hosts, (list, tuple, set, frozenset)) or not allowed_hosts:
            raise PrivacyAdapterConfigurationError("allowed_hosts must be a non-empty exact-host allowlist")
        hosts = set()
        for host in allowed_hosts:
            if not isinstance(host, str) or _HOST_RE.fullmatch(host) is None:
                raise PrivacyAdapterConfigurationError("allowed host is invalid")
            hosts.add(host.casefold())
        self.delete_url = self._validate_url(delete_url, hosts)
        self.purge_url = self._validate_url(purge_url, hosts)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= float(timeout) <= 30:
            raise PrivacyAdapterConfigurationError("timeout must be within 0.1..30 seconds")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 128 <= max_response_bytes <= 1_048_576:
            raise PrivacyAdapterConfigurationError("max_response_bytes is outside safe limits")
        self.authorization_header = authorization_header
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self.opener = opener or urllib.request.build_opener(_RejectRedirects())

    @staticmethod
    def _validate_url(value: Any, allowed_hosts: set[str]) -> str:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
            raise PrivacyAdapterConfigurationError("privacy endpoint URL is invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise PrivacyAdapterConfigurationError("privacy endpoint URL is invalid") from None
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.hostname.casefold() not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise PrivacyAdapterConfigurationError("privacy endpoint is outside the HTTPS host allowlist")
        return value

    def _request(self, *, endpoint: str, payload: dict[str, Any], request_id: str, operation: str) -> dict[str, Any]:
        if not _valid_request_id(request_id):
            return dict(_FAILURE)
        try:
            authorization = self.authorization_header()
            if (
                not isinstance(authorization, str)
                or not 1 <= len(authorization) <= 8192
                or any(ord(char) < 32 or ord(char) > 126 for char in authorization)
            ):
                return dict(_FAILURE)
            body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": request_id,
                },
                method="POST",
            )
            response = self.opener.open(request, timeout=self.timeout)
            try:
                status_code = response.getcode()
                raw = response.read(self.max_response_bytes + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            if status_code not in {200, 202} or not isinstance(raw, bytes) or len(raw) > self.max_response_bytes:
                return dict(_FAILURE)
            document = json.loads(raw.decode("utf-8"))
            result = _normalise_adapter_result(document, operation=operation)
            if status_code == 202 and result["status"] != "PENDING":
                return dict(_FAILURE)
            return result
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, TypeError, UnicodeDecodeError):
            return dict(_FAILURE)
        except Exception:
            return dict(_FAILURE)

    def delete_subject_data(self, *, subject_ref: str, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if not _valid_subject_ref(subject_ref) or not _valid_reference(project_id) or (tenant_id is not None and not _valid_reference(tenant_id)):
            return dict(_FAILURE)
        return self._request(
            endpoint=self.delete_url,
            payload={"subject_ref": subject_ref, "project_id": project_id, "tenant_id": tenant_id, "request_id": request_id},
            request_id=request_id,
            operation="delete",
        )

    def purge_expired_data(self, *, cutoff: datetime, project_id: str, tenant_id: str | None, request_id: str) -> dict[str, Any]:
        if (
            not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
            or not _valid_reference(project_id)
            or (tenant_id is not None and not _valid_reference(tenant_id))
        ):
            return dict(_FAILURE)
        return self._request(
            endpoint=self.purge_url,
            payload={"cutoff": cutoff.astimezone(timezone.utc).isoformat(), "project_id": project_id, "tenant_id": tenant_id, "request_id": request_id},
            request_id=request_id,
            operation="purge",
        )


class PrivacyRetentionJob:
    """Admin-gated one-shot retention worker for an external scheduler.

    ``principal_provider`` must return a deployment-controlled service principal
    with ``provider.access.manage``. Project/tenant scopes are fixed at worker
    construction and cannot be supplied by an untrusted HTTP caller. Invoke
    ``run_once`` from cron, a systemd timer, Kubernetes CronJob, or cloud scheduler.
    """

    def __init__(
        self,
        service: Any,
        *,
        principal_provider: Callable[[], str],
        scopes: Sequence[Mapping[str, Any]],
        retention_days: int,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not callable(getattr(service, "purge_expired_privacy_data", None)):
            raise PrivacyAdapterConfigurationError("service lacks purge_expired_privacy_data")
        if not callable(principal_provider):
            raise PrivacyAdapterConfigurationError("principal_provider must be callable")
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 1 <= retention_days <= 3650:
            raise PrivacyAdapterConfigurationError("retention_days must be within 1..3650")
        if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)) or not scopes:
            raise PrivacyAdapterConfigurationError("at least one retention scope is required")
        if len(scopes) > 256:
            raise PrivacyAdapterConfigurationError("retention scope count exceeds 256")
        if clock is not None and not callable(clock):
            raise PrivacyAdapterConfigurationError("clock must be callable")
        if request_id_factory is not None and not callable(request_id_factory):
            raise PrivacyAdapterConfigurationError("request_id_factory must be callable")
        validated: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for scope in scopes:
            if not isinstance(scope, Mapping):
                raise PrivacyAdapterConfigurationError("retention scope must be a mapping")
            project_id, tenant_id = scope.get("project_id"), scope.get("tenant_id")
            if not _valid_reference(project_id) or (tenant_id is not None and not _valid_reference(tenant_id)):
                raise PrivacyAdapterConfigurationError("retention scope identifiers are invalid")
            key = (project_id, tenant_id)
            if key in seen:
                raise PrivacyAdapterConfigurationError("duplicate retention scope")
            seen.add(key)
            validated.append({"project_id": project_id, "tenant_id": tenant_id})
        self.service = service
        self.principal_provider = principal_provider
        self.scopes = tuple(validated)
        self.retention_days = retention_days
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.request_id_factory = request_id_factory

    def run_once(self) -> dict[str, Any]:
        try:
            principal_id = self.principal_provider()
        except Exception:
            return {"status": "RETENTION_JOB_PRINCIPAL_UNAVAILABLE", "completed": False, "scope_count": len(self.scopes), "completed_count": 0, "results": []}
        if not isinstance(principal_id, str) or not principal_id.strip():
            return {"status": "RETENTION_JOB_PRINCIPAL_UNAVAILABLE", "completed": False, "scope_count": len(self.scopes), "completed_count": 0, "results": []}
        try:
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("clock must return an aware datetime")
            utc_day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff = utc_day - timedelta(days=self.retention_days)
        except Exception:
            return {"status": "RETENTION_JOB_CLOCK_INVALID", "completed": False, "scope_count": len(self.scopes), "completed_count": 0, "results": []}
        results: list[dict[str, Any]] = []
        completed_count = 0
        for index, scope in enumerate(self.scopes):
            try:
                request_id = (
                    self.request_id_factory()
                    if self.request_id_factory is not None
                    else uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "aca-privacy-retention-v1:"
                        + scope["project_id"] + ":"
                        + (scope["tenant_id"] or "no-tenant") + ":"
                        + cutoff.isoformat(),
                    ).hex
                )
                if not _valid_request_id(request_id):
                    raise ValueError("invalid request ID")
                raw = self.service.purge_expired_privacy_data(
                    cutoff=cutoff,
                    project_id=scope["project_id"],
                    tenant_id=scope["tenant_id"],
                    request_id=request_id,
                    principal_id=principal_id,
                    confirmed=True,
                )
                if not isinstance(raw, Mapping):
                    raise ValueError("invalid service response")
                completed = raw.get("confirmed") is True and raw.get("completed") is True
                status = raw.get("status")
                if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", status):
                    status = "INVALID_RETENTION_RESPONSE"
                    completed = False
                count = raw.get("deleted_count", 0)
                if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1_000_000_000_000:
                    count = 0
                    completed = False
                stores = raw.get("store_count", 0)
                if isinstance(stores, bool) or not isinstance(stores, int) or not 0 <= stores <= 256:
                    stores = 0
                    completed = False
                results.append({"scope_index": index, "status": status, "completed": completed, "store_count": stores, "deleted_count": count})
                completed_count += int(completed)
            except Exception:
                results.append({"scope_index": index, "status": "RETENTION_SCOPE_FAILED", "completed": False, "store_count": 0, "deleted_count": 0})
        all_completed = completed_count == len(self.scopes)
        return {
            "status": "RETENTION_JOB_COMPLETED" if all_completed else "RETENTION_JOB_PARTIAL",
            "completed": all_completed,
            "scope_count": len(self.scopes),
            "completed_count": completed_count,
            "cutoff": cutoff.isoformat(),
            "results": results,
        }
