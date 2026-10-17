"""Bounded in-process execution queue with explicit backpressure semantics.

Only request metadata is stored; prompts, credentials, payloads, and provider responses
must remain outside the queue. Running work retains its capacity slot after cancel or
deadline requests until its worker confirms a terminal outcome.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class BoundedExecutionQueue:
    TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"})

    def __init__(
        self,
        *,
        max_pending: int = 100,
        max_running: int = 8,
        max_pending_per_tenant: int = 25,
        max_terminal_records: int = 10000,
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        for name, value, minimum, maximum in (
            ("max_pending", max_pending, 1, 1_000_000),
            ("max_running", max_running, 1, 100_000),
            ("max_pending_per_tenant", max_pending_per_tenant, 1, 1_000_000),
            ("max_terminal_records", max_terminal_records, 1, 1_000_000),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        self.max_pending = max_pending
        self.max_running = max_running
        self.max_pending_per_tenant = max_pending_per_tenant
        if state_path is not None and not isinstance(state_path, (str, os.PathLike)):
            raise ValueError("state_path must be a path or None")
        self.max_terminal_records = max_terminal_records
        self.state_path = Path(state_path) if state_path is not None else None
        self._lock = RLock()
        self._sequence = 0
        self._last_claimed_tenant: str | None = None
        self._pending: dict[str, dict[str, Any]] = {}
        self._running: dict[str, dict[str, Any]] = {}
        self._terminal: dict[str, dict[str, Any]] = {}
        self._accepted = 0
        self._rejected = 0
        if self.state_path is not None:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self.state_path is None:
                yield
                return
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()
                yield
                self._save_state()

    def _reload_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            self._sequence = 0
            self._last_claimed_tenant = None
            self._pending = {}
            self._running = {}
            self._terminal = {}
            self._accepted = 0
            self._rejected = 0
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid execution queue state") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != 1
            or not isinstance(loaded.get("pending"), dict)
            or not isinstance(loaded.get("running"), dict)
            or not isinstance(loaded.get("terminal"), dict)
            or not isinstance(loaded.get("sequence"), int)
            or isinstance(loaded.get("sequence"), bool)
            or loaded["sequence"] < 0
            or not isinstance(loaded.get("accepted"), int)
            or isinstance(loaded.get("accepted"), bool)
            or loaded["accepted"] < 0
            or not isinstance(loaded.get("rejected"), int)
            or isinstance(loaded.get("rejected"), bool)
            or loaded["rejected"] < 0
        ):
            raise ValueError("invalid execution queue state schema")
        pending = loaded["pending"]
        running = loaded["running"]
        terminal = loaded["terminal"]
        if len(pending) > self.max_pending or len(running) > self.max_running or len(terminal) > self.max_terminal_records:
            raise ValueError("execution queue state exceeds configured limits")
        for collection, statuses in (
            (pending, {"QUEUED"}), (running, {"RUNNING"}),
            (terminal, self.TERMINAL_STATES),
        ):
            for request_id, record in collection.items():
                if (
                    not self._valid_id(request_id)
                    or not isinstance(record, dict)
                    or record.get("request_id", request_id) != request_id
                    or not self._valid_id(record.get("tenant_id"))
                    or not self._valid_id(record.get("project_id"))
                    or not self._valid_id(record.get("category"), max_length=80)
                    or not isinstance(record.get("status"), str)
                    or record["status"] not in statuses
                ):
                    raise ValueError("invalid execution queue record")
                if not isinstance(record.get("fingerprint"), list):
                    raise ValueError("invalid execution queue idempotency fingerprint")
        self._sequence = loaded["sequence"]
        self._last_claimed_tenant = loaded.get("last_claimed_tenant")
        if self._last_claimed_tenant is not None and not self._valid_id(self._last_claimed_tenant):
            raise ValueError("invalid execution queue scheduler state")
        self._pending = pending
        self._running = running
        self._terminal = terminal
        self._accepted = loaded["accepted"]
        self._rejected = loaded["rejected"]

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sequence": self._sequence,
            "last_claimed_tenant": self._last_claimed_tenant,
            "pending": self._pending,
            "running": self._running,
            "terminal": self._terminal,
            "accepted": self._accepted,
            "rejected": self._rejected,
        }
        fd, temporary = tempfile.mkstemp(
            prefix=self.state_path.name + ".", suffix=".tmp", dir=str(self.state_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _valid_id(value: Any, *, max_length: int = 128) -> bool:
        return (
            isinstance(value, str) and bool(value.strip()) and len(value) <= max_length
            and value == value.strip() and not any(ord(char) < 32 for char in value)
        )

    @staticmethod
    def _utc_now(value: datetime | None = None) -> datetime:
        current = datetime.now(timezone.utc) if value is None else value
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("time must be timezone-aware")
        return current.astimezone(timezone.utc)

    @classmethod
    def _parse_deadline(cls, value: str | None) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError("deadline must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("deadline must be an ISO-8601 timestamp") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("deadline must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _fingerprint(tenant_id: str, project_id: str, category: str, priority: int, deadline: datetime | None) -> list[Any]:
        return [tenant_id, project_id, category, priority, deadline.isoformat() if deadline else None]

    def enqueue(
        self,
        *,
        request_id: str,
        tenant_id: str,
        project_id: str,
        category: str,
        priority: int = 0,
        deadline_at: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(tenant_id) or not self._valid_id(project_id):
            return {"status": "INVALID_REQUEST_METADATA", "accepted": False}
        if not self._valid_id(category, max_length=80):
            return {"status": "INVALID_REQUEST_METADATA", "accepted": False}
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            return {"status": "INVALID_PRIORITY", "accepted": False}
        try:
            current = self._utc_now(now)
            deadline = self._parse_deadline(deadline_at)
        except ValueError:
            return {"status": "INVALID_DEADLINE", "accepted": False}
        if deadline is not None and deadline <= current:
            return {"status": "DEADLINE_ALREADY_EXPIRED", "accepted": False}
        fingerprint = self._fingerprint(tenant_id, project_id, category, priority, deadline)
        with self._transaction():
            existing = self._pending.get(request_id) or self._running.get(request_id) or self._terminal.get(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return {"status": "REQUEST_ID_CONFLICT", "accepted": False}
                if request_id in self._pending:
                    return {"status": "ALREADY_QUEUED", "accepted": True, "request_id": request_id}
                if request_id in self._running:
                    return {"status": "ALREADY_RUNNING", "accepted": True, "request_id": request_id}
                return {"status": "ALREADY_TERMINAL", "accepted": False, "request_id": request_id}
            if len(self._pending) >= self.max_pending:
                self._rejected += 1
                return {"status": "QUEUE_FULL", "accepted": False}
            tenant_pending = sum(1 for item in self._pending.values() if item["tenant_id"] == tenant_id)
            if tenant_pending >= self.max_pending_per_tenant:
                self._rejected += 1
                return {"status": "TENANT_QUEUE_LIMIT", "accepted": False}
            self._sequence += 1
            record = {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "category": category,
                "priority": priority,
                "deadline_at": deadline.isoformat() if deadline else None,
                "queued_at": current.isoformat(),
                "sequence": self._sequence,
                "fingerprint": fingerprint,
                "status": "QUEUED",
            }
            self._pending[request_id] = record
            self._accepted += 1
            return {"status": "QUEUED", "accepted": True, "request_id": request_id}

    def _terminalize(self, request_id: str, record: dict[str, Any], status: str, now: datetime) -> None:
        self._terminal[request_id] = {
            "fingerprint": record["fingerprint"],
            "tenant_id": record["tenant_id"],
            "project_id": record["project_id"],
            "category": record["category"],
            "status": status,
            "completed_at": now.isoformat(),
        }
        while len(self._terminal) > self.max_terminal_records:
            oldest = next(iter(self._terminal))
            del self._terminal[oldest]

    def _expire_pending(self, now: datetime) -> int:
        expired = 0
        for request_id, record in list(self._pending.items()):
            deadline = self._parse_deadline(record["deadline_at"])
            if deadline is not None and deadline <= now:
                del self._pending[request_id]
                self._terminalize(request_id, record, "EXPIRED", now)
                expired += 1
        return expired

    def claim_next(self, *, worker_id: str, now: datetime | None = None) -> dict[str, Any]:
        if not self._valid_id(worker_id, max_length=128):
            return {"status": "INVALID_WORKER_ID", "job": None}
        try:
            current = self._utc_now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "job": None}
        with self._transaction():
            self._expire_pending(current)
            if not self._pending:
                return {"status": "QUEUE_EMPTY", "job": None}
            if len(self._running) >= self.max_running:
                return {"status": "WORKER_CAPACITY_FULL", "job": None}
            # Fairness is across tenants first; priority/FIFO is honored within
            # each tenant so one busy tenant cannot monopolize every worker slot.
            tenant_first_sequence: dict[str, int] = {}
            for item in self._pending.values():
                tenant = item["tenant_id"]
                tenant_first_sequence[tenant] = min(
                    tenant_first_sequence.get(tenant, item["sequence"]),
                    item["sequence"],
                )
            tenant_order = sorted(tenant_first_sequence, key=tenant_first_sequence.get)
            if self._last_claimed_tenant in tenant_order:
                start = tenant_order.index(self._last_claimed_tenant) + 1
                tenant_order = tenant_order[start:] + tenant_order[:start]
            chosen_tenant = tenant_order[0]
            tenant_jobs = [
                item for item in self._pending.values()
                if item["tenant_id"] == chosen_tenant
            ]
            record = min(tenant_jobs, key=lambda item: (-item["priority"], item["sequence"]))
            self._last_claimed_tenant = chosen_tenant
            request_id = record["request_id"]
            del self._pending[request_id]
            record = dict(record)
            record.update({
                "status": "RUNNING",
                "worker_id": worker_id,
                "started_at": current.isoformat(),
                "cancel_requested": False,
                "deadline_exceeded": False,
            })
            self._running[request_id] = record
            safe_job = {
                key: record[key]
                for key in (
                    "request_id", "tenant_id", "project_id", "category", "priority",
                    "deadline_at", "queued_at", "started_at", "status",
                    "cancel_requested", "deadline_exceeded",
                )
            }
            return {"status": "CLAIMED", "job": safe_job}

    def complete(self, *, request_id: str, worker_id: str, status: str, now: datetime | None = None) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(worker_id):
            return {"status": "INVALID_REQUEST_METADATA", "completed": False}
        if not isinstance(status, str) or status not in self.TERMINAL_STATES - {"EXPIRED"}:
            return {"status": "INVALID_TERMINAL_STATUS", "completed": False}
        try:
            current = self._utc_now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "completed": False}
        with self._transaction():
            record = self._running.get(request_id)
            if record is None:
                if request_id in self._terminal:
                    return {"status": "ALREADY_TERMINAL", "completed": False}
                return {"status": "NOT_FOUND", "completed": False}
            if record["worker_id"] != worker_id:
                return {"status": "WORKER_OWNERSHIP_MISMATCH", "completed": False}
            del self._running[request_id]
            self._terminalize(request_id, record, status, current)
            return {"status": "COMPLETED", "completed": True, "terminal_status": status}

    def cancel(self, *, request_id: str, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(tenant_id):
            return {"status": "INVALID_REQUEST_METADATA", "cancelled": False}
        try:
            current = self._utc_now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "cancelled": False}
        with self._transaction():
            record = self._pending.get(request_id)
            if record is not None:
                if record["tenant_id"] != tenant_id:
                    return {"status": "TENANT_SCOPE_MISMATCH", "cancelled": False}
                del self._pending[request_id]
                self._terminalize(request_id, record, "CANCELLED", current)
                return {"status": "CANCELLED", "cancelled": True}
            record = self._running.get(request_id)
            if record is not None:
                if record["tenant_id"] != tenant_id:
                    return {"status": "TENANT_SCOPE_MISMATCH", "cancelled": False}
                record["cancel_requested"] = True
                return {"status": "CANCEL_REQUESTED", "cancelled": False}
            if request_id in self._terminal:
                return {"status": "ALREADY_TERMINAL", "cancelled": False}
            return {"status": "NOT_FOUND", "cancelled": False}

    def get_job(
        self, *, request_id: str, tenant_id: str, worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Read safe job state; running jobs are visible only to their owning worker."""
        if not self._valid_id(request_id) or not self._valid_id(tenant_id):
            return {"status": "INVALID_REQUEST_METADATA", "job": None}
        if worker_id is not None and not self._valid_id(worker_id):
            return {"status": "INVALID_WORKER_ID", "job": None}
        with self._transaction():
            record = self._pending.get(request_id)
            if record is not None:
                if record["tenant_id"] != tenant_id:
                    return {"status": "NOT_FOUND", "job": None}
            else:
                record = self._running.get(request_id)
                if record is not None:
                    if record["tenant_id"] != tenant_id:
                        return {"status": "NOT_FOUND", "job": None}
                    if worker_id != record.get("worker_id"):
                        return {"status": "WORKER_OWNERSHIP_MISMATCH", "job": None}
                else:
                    terminal = self._terminal.get(request_id)
                    if terminal is None:
                        return {"status": "NOT_FOUND", "job": None}
                    if terminal.get("tenant_id") != tenant_id:
                        return {"status": "NOT_FOUND", "job": None}
                    return {
                        "status": "TERMINAL",
                        "job": {
                            "request_id": request_id,
                            "tenant_id": tenant_id,
                            "status": terminal["status"],
                            "completed_at": terminal["completed_at"],
                        },
                    }
            keys = (
                "request_id", "tenant_id", "project_id", "category", "priority",
                "deadline_at", "queued_at", "started_at", "status",
                "cancel_requested", "deadline_exceeded",
            )
            return {"status": "FOUND", "job": {key: record[key] for key in keys if key in record}}

    def reap_expired(self, *, now: datetime | None = None) -> dict[str, Any]:
        try:
            current = self._utc_now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "pending_expired": 0, "running_deadlines_flagged": 0}
        with self._transaction():
            pending_expired = self._expire_pending(current)
            running_flagged = 0
            for record in self._running.values():
                deadline = self._parse_deadline(record["deadline_at"])
                if deadline is not None and deadline <= current and not record["deadline_exceeded"]:
                    record["deadline_exceeded"] = True
                    running_flagged += 1
            return {
                "status": "REAPED" if pending_expired or running_flagged else "NO_EXPIRED_WORK",
                "pending_expired": pending_expired,
                "running_deadlines_flagged": running_flagged,
            }

    def stats(self) -> dict[str, Any]:
        with self._transaction():
            return {
                "status": "READY",
                "pending": len(self._pending),
                "running": len(self._running),
                "max_pending": self.max_pending,
                "max_running": self.max_running,
                "max_pending_per_tenant": self.max_pending_per_tenant,
                "accepted_total": self._accepted,
                "rejected_total": self._rejected,
            }
