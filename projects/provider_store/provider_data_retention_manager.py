"""Persistent provider-data retention and deletion orchestration.

Only opaque resource references and bounded policy/audit metadata are stored.
Actual provider deletion is performed by a separately configured, authorized
provider adapter through the service facade.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z")
_DIGEST_RE = re.compile(r"[a-f0-9]{64}\Z")
_ERROR_RE = re.compile(r"[A-Z][A-Z0-9_]{1,79}\Z")
_CATEGORIES = frozenset({
    "public", "user_content", "personal", "credential",
    "financial", "health", "biometric", "confidential",
})
_STATES = {"ACTIVE", "DELETE_PENDING", "DELETING", "DELETE_FAILED", "DELETED"}
_FIELDS = {
    "record_id", "provider_id", "project_id", "tenant_id", "asset_ref",
    "data_categories", "retention_days", "policy_source", "created_at",
    "expires_at", "status", "attempts", "next_attempt_at",
    "lease_worker_id", "lease_token_digest", "lease_expires_at",
    "last_error_code", "deleted_at",
}


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class ProviderDataRetentionManager:
    """Persist expiry, deletion leases, retries, and minimal deletion tombstones."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        policy: Any,
        *,
        clock: Any = None,
        max_records: int = 100_000,
        max_attempts: int = 8,
        retry_backoff_seconds: float = 30.0,
    ) -> None:
        if not callable(getattr(policy, "evaluate", None)):
            raise ValueError("policy must support evaluate")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be within safe limits")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be within safe limits")
        if (
            isinstance(retry_backoff_seconds, bool)
            or not isinstance(retry_backoff_seconds, (int, float))
            or not 1 <= retry_backoff_seconds <= 86_400
        ):
            raise ValueError("retry_backoff_seconds must be within 1..86400")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_records = max_records
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "records": {}}
        self._reload()

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("timezone-aware clock required")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _valid_record(record_id: Any, record: Any) -> bool:
        if (
            not isinstance(record_id, str) or _ID_RE.fullmatch(record_id) is None
            or not isinstance(record, dict) or set(record) != _FIELDS
            or record.get("record_id") != record_id
            or not isinstance(record.get("provider_id"), str)
            or _ID_RE.fullmatch(record["provider_id"]) is None
            or not isinstance(record.get("project_id"), str)
            or _ID_RE.fullmatch(record["project_id"]) is None
            or (
                record.get("tenant_id") is not None
                and (
                    not isinstance(record.get("tenant_id"), str)
                    or _ID_RE.fullmatch(record["tenant_id"]) is None
                )
            )
            or (
                record.get("asset_ref") is not None
                and (
                    not isinstance(record.get("asset_ref"), str)
                    or _REF_RE.fullmatch(record["asset_ref"]) is None
                )
            )
            or not isinstance(record.get("data_categories"), list)
            or any(item not in _CATEGORIES for item in record["data_categories"])
            or len(set(record["data_categories"])) != len(record["data_categories"])
            or isinstance(record.get("retention_days"), bool)
            or not isinstance(record.get("retention_days"), int)
            or not 0 <= record["retention_days"] <= 3650
            or not isinstance(record.get("policy_source"), str)
            or not record["policy_source"].startswith("https://")
            or ProviderDataRetentionManager._parse_time(record.get("created_at")) is None
            or ProviderDataRetentionManager._parse_time(record.get("expires_at")) is None
            or ProviderDataRetentionManager._parse_time(record.get("next_attempt_at")) is None
            or record.get("status") not in _STATES
            or isinstance(record.get("attempts"), bool)
            or not isinstance(record.get("attempts"), int)
            or not 0 <= record["attempts"] <= 100
            or (
                record.get("lease_worker_id") is not None
                and (
                    not isinstance(record["lease_worker_id"], str)
                    or _ID_RE.fullmatch(record["lease_worker_id"]) is None
                )
            )
            or (
                record.get("lease_token_digest") is not None
                and (
                    not isinstance(record["lease_token_digest"], str)
                    or _DIGEST_RE.fullmatch(record["lease_token_digest"]) is None
                )
            )
            or (
                record.get("lease_expires_at") is not None
                and ProviderDataRetentionManager._parse_time(record["lease_expires_at"]) is None
            )
            or (
                record.get("last_error_code") is not None
                and (
                    not isinstance(record["last_error_code"], str)
                    or _ERROR_RE.fullmatch(record["last_error_code"]) is None
                )
            )
            or (
                record.get("deleted_at") is not None
                and ProviderDataRetentionManager._parse_time(record["deleted_at"]) is None
            )
        ):
            return False
        active_lease = (
            record.get("status") == "DELETING"
            and record.get("lease_worker_id") is not None
            and record.get("lease_token_digest") is not None
            and record.get("lease_expires_at") is not None
        )
        no_lease = all(
            record.get(field) is None
            for field in ("lease_worker_id", "lease_token_digest", "lease_expires_at")
        )
        if not (active_lease or (record.get("status") != "DELETING" and no_lease)):
            return False
        if record.get("status") == "DELETED":
            return (
                record.get("asset_ref") is None
                and record.get("data_categories") == []
                and record.get("deleted_at") is not None
            )
        return record.get("asset_ref") is not None and record.get("deleted_at") is None

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "records": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("invalid provider data-retention state JSON") from exc
        records = loaded.get("records") if isinstance(loaded, dict) else None
        if (
            not isinstance(loaded, dict)
            or set(loaded) != {"version", "records"}
            or loaded.get("version") != "1.0"
            or not isinstance(records, dict)
            or len(records) > self.max_records
            or any(not self._valid_record(key, value) for key, value in records.items())
        ):
            raise ValueError("invalid provider data-retention state schema or capacity")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def register_asset(
        self,
        record_id: str,
        *,
        provider_id: str,
        project_id: str,
        asset_ref: str,
        data_categories: list[str] | tuple[str, ...],
        retention_days: int,
        tenant_id: str | None = None,
        provider_training_use: bool = False,
        user_training_consent: bool = False,
    ) -> dict[str, Any]:
        for value, name in ((record_id, "record_id"), (provider_id, "provider_id"), (project_id, "project_id")):
            if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if tenant_id is not None and (not isinstance(tenant_id, str) or _ID_RE.fullmatch(tenant_id) is None):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        if not isinstance(asset_ref, str) or _REF_RE.fullmatch(asset_ref) is None:
            return {"status": "INVALID_ASSET_REFERENCE", "saved": False}
        if not isinstance(data_categories, (list, tuple)) or any(
            not isinstance(item, str) or item not in _CATEGORIES for item in data_categories
        ):
            return {"status": "INVALID_DATA_CATEGORIES", "saved": False}
        if len(set(data_categories)) != len(data_categories):
            return {"status": "DUPLICATE_DATA_CATEGORIES", "saved": False}
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 0 <= retention_days <= 3650:
            return {"status": "INVALID_RETENTION_PERIOD", "saved": False}
        try:
            decision = self.policy.evaluate(
                provider_id=provider_id,
                data_categories=data_categories,
                retention_days=retention_days,
                provider_training_use=provider_training_use,
                user_training_consent=user_training_consent,
            )
        except Exception as exc:
            return {"status": "DATA_POLICY_CHECK_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(decision, dict) or decision.get("allowed") is not True:
            return {
                "status": "PROVIDER_DATA_POLICY_REJECTED",
                "saved": False,
                "policy": decision if isinstance(decision, dict) else {"status": "INVALID_POLICY_RESPONSE"},
            }
        source = decision.get("policy_source")
        if not isinstance(source, str) or not source.startswith("https://"):
            return {"status": "PROVIDER_POLICY_SOURCE_INVALID", "saved": False}
        try:
            now = self._now()
            expires = now + timedelta(days=retention_days)
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "saved": False}
        record = {
            "record_id": record_id,
            "provider_id": provider_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "asset_ref": asset_ref,
            "data_categories": sorted(data_categories),
            "retention_days": retention_days,
            "policy_source": source,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "status": "ACTIVE",
            "attempts": 0,
            "next_attempt_at": expires.isoformat(),
            "lease_worker_id": None,
            "lease_token_digest": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "deleted_at": None,
        }
        with self._transaction():
            previous = self.data["records"].get(record_id)
            if previous is not None:
                same = all(previous.get(key) == record.get(key) for key in (
                    "provider_id", "project_id", "tenant_id", "asset_ref",
                    "data_categories", "retention_days", "policy_source",
                ))
                return {
                    "status": "ASSET_ALREADY_REGISTERED" if same else "RECORD_ID_CONFLICT",
                    "saved": False,
                    "record_id": record_id,
                }
            if len(self.data["records"]) >= self.max_records:
                return {"status": "RETENTION_CAPACITY_REACHED", "saved": False}
            self.data["records"][record_id] = record
            self._save()
        return {
            "status": "ASSET_REGISTERED",
            "saved": True,
            "record_id": record_id,
            "expires_at": expires.isoformat(),
            "policy_source": source,
        }

    def _advance_due_locked(self, now: datetime) -> int:
        changed = 0
        for record in self.data["records"].values():
            if record["status"] == "ACTIVE" and self._parse_time(record["expires_at"]) <= now:
                record["status"] = "DELETE_PENDING"
                record["next_attempt_at"] = now.isoformat()
                changed += 1
            elif (
                record["status"] == "DELETING"
                and self._parse_time(record["lease_expires_at"]) <= now
            ):
                record["lease_worker_id"] = None
                record["lease_token_digest"] = None
                record["lease_expires_at"] = None
                if record["attempts"] >= self.max_attempts:
                    record["status"] = "DELETE_FAILED"
                    record["last_error_code"] = "LEASE_EXPIRED_MAX_ATTEMPTS"
                else:
                    record["status"] = "DELETE_PENDING"
                    record["next_attempt_at"] = now.isoformat()
                changed += 1
        return changed

    def expire_due_records(self) -> dict[str, Any]:
        try:
            now = self._now()
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "updated": 0}
        with self._transaction():
            updated = self._advance_due_locked(now)
            if updated:
                self._save()
        return {"status": "RETENTION_EXPIRY_CHECKED", "updated": updated}

    def claim_due_deletions(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        if not isinstance(worker_id, str) or _ID_RE.fullmatch(worker_id) is None:
            return {"status": "INVALID_WORKER_ID", "items": []}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return {"status": "INVALID_BATCH_LIMIT", "items": []}
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 15 <= lease_seconds <= 3600:
            return {"status": "INVALID_LEASE_DURATION", "items": []}
        try:
            now = self._now()
            lease_expiry = now + timedelta(seconds=lease_seconds)
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "items": []}
        items: list[dict[str, Any]] = []
        with self._transaction():
            changed = self._advance_due_locked(now)
            ready = sorted(
                (
                    record for record in self.data["records"].values()
                    if record["status"] == "DELETE_PENDING"
                    and self._parse_time(record["next_attempt_at"]) <= now
                    and record["attempts"] < self.max_attempts
                ),
                key=lambda record: (record["expires_at"], record["record_id"]),
            )
            for record in ready[:limit]:
                token = secrets.token_urlsafe(32)
                record["status"] = "DELETING"
                record["attempts"] += 1
                record["lease_worker_id"] = worker_id
                record["lease_token_digest"] = _token_digest(token)
                record["lease_expires_at"] = lease_expiry.isoformat()
                record["last_error_code"] = None
                items.append({
                    "record_id": record["record_id"],
                    "provider_id": record["provider_id"],
                    "project_id": record["project_id"],
                    "tenant_id": record["tenant_id"],
                    "asset_ref": record["asset_ref"],
                    "data_categories": list(record["data_categories"]),
                    "lease_token": token,
                    "lease_expires_at": lease_expiry.isoformat(),
                })
            if changed or items:
                self._save()
        return {"status": "DELETION_CLAIMS_ISSUED", "items": items}

    def _verify_lease_locked(
        self, record_id: str, worker_id: str, lease_token: str, now: datetime
    ) -> tuple[dict[str, Any] | None, str | None]:
        record = self.data["records"].get(record_id)
        if record is None:
            return None, "RETENTION_RECORD_NOT_FOUND"
        if record["status"] != "DELETING":
            return None, "DELETION_NOT_IN_PROGRESS"
        if record["lease_worker_id"] != worker_id:
            return None, "DELETION_LEASE_OWNER_MISMATCH"
        digest = record.get("lease_token_digest")
        if (
            not isinstance(lease_token, str)
            or not 32 <= len(lease_token) <= 128
            or re.fullmatch(r"[A-Za-z0-9_-]+", lease_token) is None
            or not isinstance(digest, str)
        ):
            return None, "DELETION_LEASE_INVALID"
        if not hmac.compare_digest(digest, _token_digest(lease_token)):
            return None, "DELETION_LEASE_INVALID"
        expiry = self._parse_time(record["lease_expires_at"])
        if expiry is None or expiry <= now:
            return None, "DELETION_LEASE_EXPIRED"
        return record, None

    @staticmethod
    def _clear_lease(record: dict[str, Any]) -> None:
        record["lease_worker_id"] = None
        record["lease_token_digest"] = None
        record["lease_expires_at"] = None

    def complete_deletion(
        self, record_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        if not isinstance(record_id, str) or _ID_RE.fullmatch(record_id) is None:
            return {"status": "INVALID_RECORD_ID", "completed": False}
        if not isinstance(worker_id, str) or _ID_RE.fullmatch(worker_id) is None:
            return {"status": "INVALID_WORKER_ID", "completed": False}
        try:
            now = self._now()
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "completed": False}
        with self._transaction():
            record, error = self._verify_lease_locked(record_id, worker_id, lease_token, now)
            if error:
                return {"status": error, "completed": False}
            record["status"] = "DELETED"
            record["asset_ref"] = None
            record["data_categories"] = []
            record["deleted_at"] = now.isoformat()
            record["last_error_code"] = None
            self._clear_lease(record)
            self._save()
        return {"status": "DATA_DELETION_CONFIRMED", "completed": True, "record_id": record_id}

    def fail_deletion(
        self,
        record_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_code: str = "PROVIDER_DELETE_FAILED",
    ) -> dict[str, Any]:
        if not isinstance(record_id, str) or _ID_RE.fullmatch(record_id) is None:
            return {"status": "INVALID_RECORD_ID", "completed": False}
        if not isinstance(worker_id, str) or _ID_RE.fullmatch(worker_id) is None:
            return {"status": "INVALID_WORKER_ID", "completed": False}
        if not isinstance(error_code, str) or _ERROR_RE.fullmatch(error_code) is None or any(
            term in error_code.casefold() for term in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
        ):
            error_code = "PROVIDER_DELETE_FAILED"
        try:
            now = self._now()
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "completed": False}
        with self._transaction():
            record, error = self._verify_lease_locked(record_id, worker_id, lease_token, now)
            if error:
                return {"status": error, "completed": False}
            record["last_error_code"] = error_code
            self._clear_lease(record)
            if record["attempts"] >= self.max_attempts:
                record["status"] = "DELETE_FAILED"
                retry_at = None
            else:
                record["status"] = "DELETE_PENDING"
                delay = min(self.retry_backoff_seconds * (2 ** min(record["attempts"] - 1, 20)), 86_400)
                retry_at = now + timedelta(seconds=delay)
                record["next_attempt_at"] = retry_at.isoformat()
            self._save()
        return {
            "status": "DELETION_RETRY_SCHEDULED" if retry_at else "DELETION_RETRY_LIMIT_REACHED",
            "completed": False,
            "record_id": record_id,
            "next_attempt_at": retry_at.isoformat() if retry_at else None,
        }

    def requeue_failed(self, record_id: str) -> dict[str, Any]:
        if not isinstance(record_id, str) or _ID_RE.fullmatch(record_id) is None:
            return {"status": "INVALID_RECORD_ID", "requeued": False}
        try:
            now = self._now()
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "requeued": False}
        with self._transaction():
            record = self.data["records"].get(record_id)
            if record is None:
                return {"status": "RETENTION_RECORD_NOT_FOUND", "requeued": False}
            if record["status"] != "DELETE_FAILED":
                return {"status": "RECORD_NOT_FAILED", "requeued": False}
            record["status"] = "DELETE_PENDING"
            record["attempts"] = 0
            record["next_attempt_at"] = now.isoformat()
            record["last_error_code"] = None
            self._save()
        return {"status": "DELETION_MANUALLY_REQUEUED", "requeued": True, "record_id": record_id}

    def get_record(self, record_id: str, *, tenant_id: str | None = None) -> dict[str, Any]:
        if not isinstance(record_id, str) or _ID_RE.fullmatch(record_id) is None:
            return {"status": "INVALID_RECORD_ID", "record": None}
        with self._transaction():
            record = self.data["records"].get(record_id)
            if record is None or record.get("tenant_id") != tenant_id:
                return {"status": "RETENTION_RECORD_NOT_FOUND", "record": None}
            safe = {
                key: record[key] for key in (
                    "record_id", "provider_id", "project_id", "tenant_id",
                    "retention_days", "policy_source", "created_at", "expires_at",
                    "status", "attempts", "next_attempt_at", "last_error_code",
                    "deleted_at",
                )
            }
        return {"status": "RETENTION_RECORD_FOUND", "record": safe}

    def status(self) -> dict[str, Any]:
        with self._transaction():
            counts = {state: 0 for state in sorted(_STATES)}
            for record in self.data["records"].values():
                counts[record["status"]] += 1
            return {
                "status": "RETENTION_STATUS",
                "total_records": len(self.data["records"]),
                "counts": counts,
            }
