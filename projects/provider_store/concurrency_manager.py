"""Durable provider/project/global execution admission limits.

Leases are not expired automatically: a STARTED or UNKNOWN request may still be
running remotely. An authorized operator must reconcile ambiguous leases explicitly.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_ACTIVE = {"RESERVED", "STARTED", "UNKNOWN"}
_TERMINAL = {"COMPLETED", "NOT_STARTED", "RECONCILED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_limit(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100000


class ProviderConcurrencyManager:
    """Enforce provider-specific, per-project, and global active-request limits."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        default_provider_limit: int = 4,
        global_limit: int = 32,
        project_limit: int = 8,
        max_entries: int = 10000,
    ) -> None:
        if not _valid_limit(default_provider_limit):
            raise ValueError("default_provider_limit must be a positive integer")
        if not _valid_limit(global_limit):
            raise ValueError("global_limit must be a positive integer")
        if not _valid_limit(project_limit):
            raise ValueError("project_limit must be a positive integer")
        if not _valid_limit(max_entries):
            raise ValueError("max_entries must be a positive integer")
        self.path = Path(path)
        self.default_provider_limit = default_provider_limit
        self.global_limit = global_limit
        self.project_limit = project_limit
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "provider_limits": {}, "leases": {}}
        with self._transaction():
            pass

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            with interprocess_file_lock(self.path.with_name(self.path.name + ".lock")):
                self._reload()
                yield

    def _reload(self) -> None:
        if not self.path.exists():
            self.data = {"version": "1.0", "provider_limits": {}, "leases": {}}
            return
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider concurrency state") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("provider_limits"), dict)
            or not isinstance(loaded.get("leases"), dict)
            or len(loaded["leases"]) > self.max_entries
        ):
            raise ValueError("invalid provider concurrency state")
        for provider_id, limit in loaded["provider_limits"].items():
            if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id) or not _valid_limit(limit):
                raise ValueError("invalid provider concurrency limit")
        for request_id, lease in loaded["leases"].items():
            if (
                not isinstance(request_id, str)
                or not _ID_RE.fullmatch(request_id)
                or not isinstance(lease, dict)
                or not isinstance(lease.get("status"), str)
                or lease.get("status") not in _ACTIVE | _TERMINAL
                or not isinstance(lease.get("provider_id"), str)
                or not _ID_RE.fullmatch(lease["provider_id"])
                or not isinstance(lease.get("project_id"), str)
                or not _ID_RE.fullmatch(lease["project_id"])
                or not isinstance(lease.get("created_at"), str)
                or not isinstance(lease.get("updated_at"), str)
            ):
                raise ValueError("invalid provider concurrency lease")
            for key in ("created_at", "updated_at"):
                try:
                    datetime.fromisoformat(lease[key])
                except ValueError as exc:
                    raise ValueError("invalid provider concurrency timestamp") from exc
            outcome = lease.get("outcome")
            if outcome is not None and (
                not isinstance(outcome, str)
                or outcome not in {"completed", "not_started", "unknown", "reconciled"}
            ):
                raise ValueError("invalid provider concurrency outcome")
        self.data = loaded

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def set_provider_limit(self, provider_id: str, limit: int) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not _valid_limit(limit):
            return {"status": "INVALID_CONCURRENCY_LIMIT", "saved": False}
        with self._transaction():
            self.data["provider_limits"][provider_id] = limit
            self._save()
            return {"status": "LIMIT_UPDATED", "saved": True, "provider_id": provider_id, "limit": limit}

    def get_provider_limit(self, provider_id: str) -> int:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            raise ValueError("invalid provider ID")
        with self._transaction():
            return self.data["provider_limits"].get(provider_id, self.default_provider_limit)

    def acquire(
        self,
        *,
        provider_id: str,
        project_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "acquired": False}
        if not isinstance(project_id, str) or not _ID_RE.fullmatch(project_id):
            return {"status": "INVALID_PROJECT_ID", "acquired": False}
        if not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "acquired": False}
        with self._transaction():
            leases = self.data["leases"]
            existing = leases.get(request_id)
            if isinstance(existing, dict):
                if existing.get("provider_id") != provider_id or existing.get("project_id") != project_id:
                    return {"status": "REQUEST_ID_CONFLICT", "acquired": False}
                if existing.get("status") in _ACTIVE:
                    return {
                        "status": "LEASE_ALREADY_ACTIVE", "acquired": False,
                        "lease_id": request_id, "lease_status": existing["status"],
                    }
                return {
                    "status": "DUPLICATE_LEASE", "acquired": False,
                        "lease_id": request_id, "lease_status": existing["status"],
                }
            if len(leases) >= self.max_entries:
                return {"status": "CONCURRENCY_LEDGER_CAPACITY_REACHED", "acquired": False}
            active = [item for item in leases.values() if item.get("status") in _ACTIVE]
            if len(active) >= self.global_limit:
                return {"status": "GLOBAL_CONCURRENCY_LIMIT_REACHED", "acquired": False}
            if sum(item.get("project_id") == project_id for item in active) >= self.project_limit:
                return {"status": "PROJECT_CONCURRENCY_LIMIT_REACHED", "acquired": False}
            provider_limit = self.data["provider_limits"].get(provider_id, self.default_provider_limit)
            if sum(item.get("provider_id") == provider_id for item in active) >= provider_limit:
                return {
                    "status": "PROVIDER_CONCURRENCY_LIMIT_REACHED",
                    "acquired": False,
                    "provider_id": provider_id,
                    "limit": provider_limit,
                }
            now = _now()
            leases[request_id] = {
                "provider_id": provider_id,
                "project_id": project_id,
                "status": "RESERVED",
                "outcome": None,
                "created_at": now,
                "updated_at": now,
            }
            self._save()
            return {"status": "LEASE_ACQUIRED", "acquired": True, "lease_id": request_id}

    def mark_started(self, *, lease_id: str) -> dict[str, Any]:
        if not isinstance(lease_id, str) or not _ID_RE.fullmatch(lease_id):
            return {"status": "INVALID_LEASE_ID", "saved": False}
        with self._transaction():
            lease = self.data["leases"].get(lease_id)
            if not isinstance(lease, dict) or lease.get("status") != "RESERVED":
                return {"status": "LEASE_NOT_RESERVED", "saved": False}
            lease["status"] = "STARTED"
            lease["updated_at"] = _now()
            self._save()
            return {"status": "LEASE_STARTED", "saved": True, "lease_id": lease_id}

    def release(self, *, lease_id: str, outcome: str) -> dict[str, Any]:
        if not isinstance(lease_id, str) or not _ID_RE.fullmatch(lease_id):
            return {"status": "INVALID_LEASE_ID", "saved": False}
        if not isinstance(outcome, str) or outcome not in {"completed", "not_started", "unknown"}:
            return {"status": "INVALID_LEASE_OUTCOME", "saved": False}
        with self._transaction():
            lease = self.data["leases"].get(lease_id)
            if not isinstance(lease, dict):
                return {"status": "LEASE_NOT_FOUND", "saved": False}
            if lease["status"] in _TERMINAL:
                if lease.get("outcome") == outcome:
                    return {"status": "ALREADY_RELEASED", "saved": True, "duplicate": True}
                return {"status": "LEASE_TERMINAL_CONFLICT", "saved": False}
            if outcome == "unknown":
                lease["status"] = "UNKNOWN"
                lease["outcome"] = "unknown"
                lease["updated_at"] = _now()
                self._save()
                return {"status": "LEASE_RETAINED_UNKNOWN", "saved": True, "retained": True}
            if outcome == "completed" and lease["status"] != "STARTED":
                return {"status": "INVALID_LEASE_TRANSITION", "saved": False}
            if outcome == "not_started" and lease["status"] not in {"RESERVED", "STARTED"}:
                return {"status": "INVALID_LEASE_TRANSITION", "saved": False}
            lease["status"] = "COMPLETED" if outcome == "completed" else "NOT_STARTED"
            lease["outcome"] = outcome
            lease["updated_at"] = _now()
            self._save()
            return {"status": "LEASE_RELEASED", "saved": True, "retained": False}

    def reconcile(
        self,
        *,
        lease_id: str,
        outcome: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        """Release an ambiguous lease only after an authorized operator confirms the remote job stopped."""
        if not isinstance(lease_id, str) or not _ID_RE.fullmatch(lease_id):
            return {"status": "INVALID_LEASE_ID", "saved": False}
        if not isinstance(confirmed, bool) or confirmed is not True:
            return {"status": "CONFIRMATION_REQUIRED", "saved": False}
        if not isinstance(outcome, str) or outcome not in {"completed", "not_running"}:
            return {"status": "INVALID_RECONCILIATION_OUTCOME", "saved": False}
        with self._transaction():
            lease = self.data["leases"].get(lease_id)
            if not isinstance(lease, dict):
                return {"status": "LEASE_NOT_FOUND", "saved": False}
            if lease["status"] not in {"STARTED", "UNKNOWN"}:
                return {"status": "LEASE_NOT_AMBIGUOUS", "saved": False}
            lease["status"] = "RECONCILED"
            lease["outcome"] = "reconciled"
            lease["reconciliation"] = {
                "outcome": outcome,
                "confirmed": True,
                "at": _now(),
            }
            lease["updated_at"] = _now()
            self._save()
            return {"status": "LEASE_RECONCILED", "saved": True, "retained": False}

    def get(self, lease_id: str) -> dict[str, Any]:
        if not isinstance(lease_id, str) or not _ID_RE.fullmatch(lease_id):
            return {"status": "INVALID_LEASE_ID"}
        with self._transaction():
            lease = self.data["leases"].get(lease_id)
            if not isinstance(lease, dict):
                return {"status": "NOT_FOUND", "lease_id": lease_id}
            return {"status": "FOUND", "lease": dict(lease)}

    def summary(self) -> dict[str, int]:
        with self._transaction():
            result = {status: 0 for status in sorted(_ACTIVE | _TERMINAL)}
            for lease in self.data["leases"].values():
                status = lease["status"]
                result[status] += 1
            return result
