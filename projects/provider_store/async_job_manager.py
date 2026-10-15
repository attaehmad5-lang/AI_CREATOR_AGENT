"""In-process async job lifecycle and tenant-scoped polling over BoundedExecutionQueue.

Job records contain lifecycle metadata and safe result codes only; output bytes, prompts,
credentials, and raw provider error messages are not stored. Durable recovery remains
the responsibility of a persistent job store/worker service.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from .execution_queue import BoundedExecutionQueue


class AsyncJobManager:
    TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"})
    SAFE_RESULT_CODE = re.compile(r"[A-Z0-9_.:-]{1,80}")

    def __init__(self, queue: BoundedExecutionQueue, *, max_job_records: int = 10000) -> None:
        if not isinstance(queue, BoundedExecutionQueue):
            raise ValueError("queue must be a BoundedExecutionQueue")
        if (
            not isinstance(max_job_records, int) or isinstance(max_job_records, bool)
            or not 1 <= max_job_records <= 1_000_000
        ):
            raise ValueError("max_job_records must be an integer between 1 and 1000000")
        self.queue = queue
        self.max_job_records = max_job_records
        self._lock = RLock()
        self._jobs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _valid_id(value: Any, *, maximum: int = 128) -> bool:
        return (
            isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and value == value.strip() and not any(ord(char) < 32 for char in value)
        )

    @staticmethod
    def _now(value: datetime | None = None) -> datetime:
        current = datetime.now(timezone.utc) if value is None else value
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("time must be timezone-aware")
        return current.astimezone(timezone.utc)

    @classmethod
    def _deadline(cls, value: str | None) -> str | None:
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
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _fingerprint(tenant_id: str, project_id: str, category: str, priority: int, deadline_at: str | None) -> tuple[Any, ...]:
        return tenant_id, project_id, category, priority, deadline_at

    def submit(
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
        if not all(self._valid_id(value) for value in (request_id, tenant_id, project_id)):
            return {"status": "INVALID_JOB_METADATA", "accepted": False}
        if not self._valid_id(category, maximum=80):
            return {"status": "INVALID_JOB_METADATA", "accepted": False}
        if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
            return {"status": "INVALID_PRIORITY", "accepted": False}
        try:
            current = self._now(now)
            normalized_deadline = self._deadline(deadline_at)
        except ValueError:
            return {"status": "INVALID_DEADLINE", "accepted": False}
        fingerprint = self._fingerprint(tenant_id, project_id, category, priority, normalized_deadline)
        with self._lock:
            existing = self._jobs.get(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return {"status": "REQUEST_ID_CONFLICT", "accepted": False}
                if existing["status"] in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
                    return {"status": "ALREADY_EXISTS", "accepted": True, "request_id": request_id}
                return {"status": "ALREADY_TERMINAL", "accepted": False, "request_id": request_id}
            if len(self._jobs) >= self.max_job_records:
                return {"status": "JOB_LEDGER_FULL", "accepted": False}
            result = self.queue.enqueue(
                request_id=request_id,
                tenant_id=tenant_id,
                project_id=project_id,
                category=category,
                priority=priority,
                deadline_at=normalized_deadline,
                now=current,
            )
            if result.get("status") != "QUEUED":
                return {"status": result.get("status", "QUEUE_REJECTED"), "accepted": False}
            self._jobs[request_id] = {
                "request_id": request_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "category": category,
                "priority": priority,
                "deadline_at": normalized_deadline,
                "fingerprint": fingerprint,
                "status": "QUEUED",
                "created_at": current.isoformat(),
                "started_at": None,
                "completed_at": None,
                "worker_id": None,
                "cancel_requested": False,
                "deadline_exceeded": False,
                "result_code": None,
            }
            return {"status": "QUEUED", "accepted": True, "request_id": request_id}

    def claim_next(self, *, worker_id: str, now: datetime | None = None) -> dict[str, Any]:
        try:
            current = self._now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "job": None}
        result = self.queue.claim_next(worker_id=worker_id, now=current)
        if result.get("status") != "CLAIMED" or not isinstance(result.get("job"), dict):
            return result
        request_id = result["job"].get("request_id")
        with self._lock:
            record = self._jobs.get(request_id)
            if record is None or record.get("status") != "QUEUED":
                # A queue/ledger mismatch is not safe to execute.
                self.queue.complete(request_id=request_id, worker_id=worker_id, status="FAILED", now=current)
                return {"status": "JOB_LEDGER_INCONSISTENT", "job": None}
            record["status"] = "RUNNING"
            record["worker_id"] = worker_id
            record["started_at"] = current.isoformat()
            return {
                "status": "CLAIMED",
                "job": {
                    "request_id": record["request_id"],
                    "tenant_id": record["tenant_id"],
                    "project_id": record["project_id"],
                    "category": record["category"],
                    "priority": record["priority"],
                    "deadline_at": record["deadline_at"],
                    "status": record["status"],
                    "started_at": record["started_at"],
                },
            }

    def complete(
        self,
        *,
        request_id: str,
        worker_id: str,
        status: str,
        result_code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(worker_id):
            return {"status": "INVALID_JOB_METADATA", "completed": False}
        if not isinstance(status, str) or status not in self.TERMINAL_STATES - {"EXPIRED"}:
            return {"status": "INVALID_TERMINAL_STATUS", "completed": False}
        if result_code is not None and (
            not isinstance(result_code, str) or not self.SAFE_RESULT_CODE.fullmatch(result_code)
            or any(term in result_code.casefold() for term in ("token", "secret", "password", "credential", "bearer"))
        ):
            return {"status": "INVALID_RESULT_CODE", "completed": False}
        try:
            current = self._now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "completed": False}
        with self._lock:
            record = self._jobs.get(request_id)
            if record is None:
                return {"status": "JOB_NOT_FOUND", "completed": False}
            if record["status"] in self.TERMINAL_STATES:
                return {"status": "ALREADY_TERMINAL", "completed": False}
            if record.get("worker_id") != worker_id or record["status"] not in {"RUNNING", "CANCEL_REQUESTED"}:
                return {"status": "WORKER_OWNERSHIP_MISMATCH", "completed": False}
            queue_result = self.queue.complete(
                request_id=request_id, worker_id=worker_id, status=status, now=current
            )
            if queue_result.get("completed") is not True:
                return {"status": queue_result.get("status", "QUEUE_COMPLETION_FAILED"), "completed": False}
            record["status"] = status
            record["completed_at"] = current.isoformat()
            record["result_code"] = result_code
            return {"status": "COMPLETED", "completed": True, "terminal_status": status}

    def poll(self, *, request_id: str, tenant_id: str) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(tenant_id):
            return {"status": "INVALID_JOB_METADATA"}
        with self._lock:
            record = self._jobs.get(request_id)
            if record is None:
                return {"status": "JOB_NOT_FOUND"}
            if record["tenant_id"] != tenant_id:
                return {"status": "TENANT_SCOPE_MISMATCH"}
            return {
                "status": "JOB_STATUS",
                "job": {
                    key: record[key]
                    for key in (
                        "request_id", "tenant_id", "project_id", "category", "priority",
                        "deadline_at", "status", "created_at", "started_at", "completed_at",
                        "cancel_requested", "deadline_exceeded", "result_code",
                    )
                },
            }

    def cancel(self, *, request_id: str, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
        if not self._valid_id(request_id) or not self._valid_id(tenant_id):
            return {"status": "INVALID_JOB_METADATA", "cancelled": False}
        try:
            current = self._now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "cancelled": False}
        with self._lock:
            record = self._jobs.get(request_id)
            if record is None:
                return {"status": "JOB_NOT_FOUND", "cancelled": False}
            if record["tenant_id"] != tenant_id:
                return {"status": "TENANT_SCOPE_MISMATCH", "cancelled": False}
            if record["status"] in self.TERMINAL_STATES:
                return {"status": "ALREADY_TERMINAL", "cancelled": False}
            result = self.queue.cancel(request_id=request_id, tenant_id=tenant_id, now=current)
            if result.get("status") == "CANCELLED":
                record["status"] = "CANCELLED"
                record["completed_at"] = current.isoformat()
                return {"status": "CANCELLED", "cancelled": True}
            if result.get("status") == "CANCEL_REQUESTED":
                record["status"] = "CANCEL_REQUESTED"
                record["cancel_requested"] = True
                return {"status": "CANCEL_REQUESTED", "cancelled": False}
            return {"status": result.get("status", "CANCEL_FAILED"), "cancelled": False}

    def reap_expired(self, *, now: datetime | None = None) -> dict[str, Any]:
        try:
            current = self._now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "expired": 0, "running_deadlines_flagged": 0}
        with self._lock:
            result = self.queue.reap_expired(now=current)
            expired = 0
            running_flagged = 0
            for record in self._jobs.values():
                deadline_text = self._deadline(record["deadline_at"])
                deadline = datetime.fromisoformat(deadline_text) if deadline_text is not None else None
                if deadline is None or deadline > current:
                    continue
                if record["status"] == "QUEUED":
                    record["status"] = "EXPIRED"
                    record["completed_at"] = current.isoformat()
                    expired += 1
                elif record["status"] in {"RUNNING", "CANCEL_REQUESTED"} and not record["deadline_exceeded"]:
                    record["deadline_exceeded"] = True
                    running_flagged += 1
            return {
                "status": result.get("status", "REAPED"),
                "expired": expired,
                "running_deadlines_flagged": running_flagged,
            }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {state: 0 for state in ("QUEUED", "RUNNING", "CANCEL_REQUESTED", *self.TERMINAL_STATES)}
            for record in self._jobs.values():
                if record["status"] in counts:
                    counts[record["status"]] += 1
            return {
                "status": "READY",
                "total_jobs": len(self._jobs),
                "states": counts,
                "queue": self.queue.stats(),
                "max_job_records": self.max_job_records,
            }
