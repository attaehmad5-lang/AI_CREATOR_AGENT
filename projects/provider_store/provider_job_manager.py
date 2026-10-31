"""Persistent asynchronous provider job queue with bounded backpressure."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z")
_ERROR_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class ProviderJobManager:
    """Queue job references only; never persist raw prompts, credentials, or output bodies."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_queue_size: int = 100,
        max_concurrent: int = 4,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        backpressure_retry_seconds: int = 5,
        clock: Any = None,
    ) -> None:
        for value, name, minimum, maximum in (
            (max_queue_size, "max_queue_size", 1, 100000),
            (max_concurrent, "max_concurrent", 1, 10000),
            (lease_seconds, "lease_seconds", 1, 86400),
            (max_attempts, "max_attempts", 1, 20),
            (backpressure_retry_seconds, "backpressure_retry_seconds", 1, 3600),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_queue_size = max_queue_size
        self.max_concurrent = max_concurrent
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backpressure_retry_seconds = backpressure_retry_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "jobs": {}}
        self._reload()

    def _now(self) -> datetime:
        """Return an aware UTC timestamp; reject broken injected clocks."""
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider job clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _reload(self) -> None:
        # Never read through a caller-controlled state-file symlink. In
        # particular, do this before exists(), which follows symlinks.
        if self.state_path.is_symlink():
            raise ValueError("provider job state must not be a symlink")
        if not self.state_path.exists():
            self.data = {"version": "1.0", "jobs": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider job state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("jobs"), dict)
        ):
            raise ValueError("invalid provider job state schema")
        for job_id, job in loaded["jobs"].items():
            if not isinstance(job, dict):
                raise ValueError("invalid provider job record")
            status = job.get("status")
            if (
                (status == "RUNNING" and (
                    not isinstance(job.get("worker_id"), str)
                    or not _ID_RE.fullmatch(job["worker_id"])
                    or not isinstance(job.get("lease_until"), str)
                ))
                or (status != "RUNNING" and (
                    job.get("worker_id") is not None
                    or job.get("lease_until") is not None
                ))
                or (isinstance(status, str) and status in _TERMINAL and not isinstance(job.get("completed_at"), str))
                or (not (isinstance(status, str) and status in _TERMINAL) and job.get("completed_at") is not None)
                or (status == "QUEUED" and job.get("attempts", 0) == 0 and job.get("started_at") is not None)
                or (status == "RUNNING" and (not isinstance(job.get("started_at"), str) or isinstance(job.get("attempts"), bool) or not isinstance(job.get("attempts"), int) or job["attempts"] < 1))
                or (status == "CANCELLED" and job.get("cancel_requested") not in (False, True))
            ):
                raise ValueError("invalid provider job lifecycle metadata")
            timestamp_fields = ["created_at", "updated_at"]
            if job.get("started_at") is not None:
                timestamp_fields.append("started_at")
            if job.get("completed_at") is not None:
                timestamp_fields.append("completed_at")
            if job.get("lease_until") is not None:
                timestamp_fields.append("lease_until")
            for field in timestamp_fields:
                value = job.get(field)
                if not isinstance(value, str):
                    raise ValueError("invalid provider job timestamp")
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("invalid provider job timestamp") from exc
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("provider job timestamps must include a timezone")
            if (
                not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
                or not isinstance(job, dict)
                or job.get("job_id") != job_id
                or not isinstance(job.get("status"), str)
                or job["status"] not in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}
                or not isinstance(job.get("project_id"), str)
                or not _ID_RE.fullmatch(job["project_id"])
                or not isinstance(job.get("provider_id"), str)
                or not _ID_RE.fullmatch(job["provider_id"])
                or not isinstance(job.get("payload_ref"), str)
                or not _REF_RE.fullmatch(job["payload_ref"])
                or isinstance(job.get("attempts"), bool)
                or not isinstance(job.get("attempts"), int) or job["attempts"] < 0
                or isinstance(job.get("priority"), bool)
                or not isinstance(job.get("priority"), int) or not 0 <= job["priority"] <= 100
                or not isinstance(job.get("cancel_requested", False), bool)
                or (
                    job.get("worker_id") is not None
                    and (
                        not isinstance(job.get("worker_id"), str)
                        or not _ID_RE.fullmatch(job["worker_id"])
                    )
                )
                or (
                    job.get("result_ref") is not None
                    and (
                        not isinstance(job.get("result_ref"), str)
                        or not _REF_RE.fullmatch(job["result_ref"])
                    )
                )
                or (
                    job.get("error_code") is not None
                    and (
                        not isinstance(job.get("error_code"), str)
                        or not _ERROR_RE.fullmatch(job["error_code"])
                    )
                )
                or (
                    job.get("last_error_code") is not None
                    and (
                        not isinstance(job.get("last_error_code"), str)
                        or not _ERROR_RE.fullmatch(job["last_error_code"])
                    )
                )
                or (
                    job.get("status") == "SUCCEEDED"
                    and (not isinstance(job.get("result_ref"), str) or not _REF_RE.fullmatch(job["result_ref"]))
                )
                or (
                    job.get("status") != "SUCCEEDED"
                    and job.get("result_ref") is not None
                )
            ):
                raise ValueError("invalid provider job record")
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
        if self.state_path.is_symlink():
            raise ValueError("provider job state must not be a symlink")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
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

    def submit(
        self,
        *,
        job_id: str,
        project_id: str,
        provider_id: str,
        payload_ref: str,
        priority: int = 50,
    ) -> dict[str, Any]:
        for value, name in ((job_id, "job_id"), (project_id, "project_id"), (provider_id, "provider_id")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "accepted": False}
        if not isinstance(payload_ref, str) or not _REF_RE.fullmatch(payload_ref):
            return {"status": "INVALID_PAYLOAD_REFERENCE", "accepted": False}
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            return {"status": "INVALID_PRIORITY", "accepted": False}
        now = self._now().isoformat()
        with self._transaction():
            existing = self.data["jobs"].get(job_id)
            if isinstance(existing, dict):
                same = all(existing.get(key) == value for key, value in {
                    "project_id": project_id, "provider_id": provider_id,
                    "payload_ref": payload_ref, "priority": priority,
                }.items())
                return {
                    "status": "ALREADY_SUBMITTED" if same else "REQUEST_ID_CONFLICT",
                    "accepted": same,
                    "job": dict(existing),
                }
            queued = sum(
                1 for job in self.data["jobs"].values()
                if isinstance(job, dict) and job.get("status") == "QUEUED"
            )
            if queued >= self.max_queue_size:
                return {
                    "status": "QUEUE_FULL",
                    "accepted": False,
                    "retry_after_seconds": self.backpressure_retry_seconds,
                }
            job = {
                "job_id": job_id, "project_id": project_id, "provider_id": provider_id,
                "payload_ref": payload_ref, "priority": priority, "status": "QUEUED",
                "attempts": 0, "created_at": now, "updated_at": now,
                "cancel_requested": False,
            }
            self.data["jobs"][job_id] = job
            self._save()
            return {"status": "JOB_QUEUED", "accepted": True, "job": dict(job)}

    def _reap_expired_locked(self, now: datetime) -> bool:
        changed = False
        for job in self.data["jobs"].values():
            if not isinstance(job, dict) or job.get("status") != "RUNNING":
                continue
            lease_until = job.get("lease_until")
            try:
                expired = isinstance(lease_until, str) and datetime.fromisoformat(lease_until) <= now
            except ValueError:
                expired = True
            if not expired:
                continue
            job["updated_at"] = now.isoformat()
            job["worker_id"] = None
            job["lease_until"] = None
            if job.get("cancel_requested") is True:
                job["status"] = "CANCELLED"
                job["completed_at"] = now.isoformat()
            elif job.get("attempts", 0) < self.max_attempts:
                job["status"] = "QUEUED"
                job["last_error_code"] = "WORKER_LEASE_EXPIRED"
            else:
                job["status"] = "FAILED"
                job["error_code"] = "WORKER_LEASE_EXPIRED"
                job["completed_at"] = now.isoformat()
            changed = True
        return changed

    def claim_next(self, *, worker_id: str) -> dict[str, Any]:
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "job": None}
        now = self._now()
        with self._transaction():
            changed = self._reap_expired_locked(now)
            running = sum(
                1 for job in self.data["jobs"].values()
                if isinstance(job, dict) and job.get("status") == "RUNNING"
            )
            if running >= self.max_concurrent:
                if changed:
                    self._save()
                return {"status": "CONCURRENCY_LIMIT", "job": None}
            queued = [
                job for job in self.data["jobs"].values()
                if isinstance(job, dict) and job.get("status") == "QUEUED"
            ]
            queued.sort(key=lambda job: (-job["priority"], job["created_at"], job["job_id"]))
            if not queued:
                if changed:
                    self._save()
                return {"status": "QUEUE_EMPTY", "job": None}
            job = queued[0]
            job["status"] = "RUNNING"
            job["worker_id"] = worker_id
            job["attempts"] += 1
            job["started_at"] = job.get("started_at") or now.isoformat()
            job["updated_at"] = now.isoformat()
            job["lease_until"] = (now + timedelta(seconds=self.lease_seconds)).isoformat()
            self._save()
            return {"status": "JOB_CLAIMED", "job": dict(job)}

    def complete(
        self,
        *,
        job_id: str,
        worker_id: str,
        success: bool,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "saved": False}
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        if success and (not isinstance(result_ref, str) or not _REF_RE.fullmatch(result_ref)):
            return {"status": "INVALID_RESULT_REFERENCE", "saved": False}
        if not success and error_code is not None and (
            not isinstance(error_code, str) or not _ERROR_RE.fullmatch(error_code)
        ):
            return {"status": "INVALID_ERROR_CODE", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if job.get("status") != "RUNNING" or job.get("worker_id") != worker_id:
                return {"status": "JOB_NOT_OWNED_BY_WORKER", "saved": False}
            if job.get("cancel_requested") is True:
                job["status"] = "CANCELLED"
            else:
                job["status"] = "SUCCEEDED" if success else "FAILED"
                if success:
                    job["result_ref"] = result_ref
                elif error_code:
                    job["error_code"] = error_code
            job["completed_at"] = now
            job["updated_at"] = now
            job["lease_until"] = None
            job["worker_id"] = None
            self._save()
            return {"status": "JOB_COMPLETED", "saved": True, "job": dict(job)}

    def cancel(self, *, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if job.get("status") == "QUEUED":
                job["status"] = "CANCELLED"
                job["completed_at"] = now
            elif job.get("status") == "RUNNING":
                job["cancel_requested"] = True
            else:
                return {"status": "JOB_TERMINAL", "saved": False}
            job["updated_at"] = now
            self._save()
            return {"status": "CANCEL_REQUESTED" if job["status"] == "RUNNING" else "JOB_CANCELLED", "saved": True, "job": dict(job)}

    def poll(self, *, job_id: str) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "job": None}
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "job": None}
            return {"status": "JOB_STATUS", "job": dict(job)}
