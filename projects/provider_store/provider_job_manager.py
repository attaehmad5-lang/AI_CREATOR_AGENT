"""Persistent asynchronous provider job queue with bounded backpressure."""
from __future__ import annotations

import hashlib
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
_ERROR_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
_REMOTE_SUBMISSION_STATES = {"NOT_SUBMITTED", "SUBMITTING", "UNKNOWN", "SUBMITTED"}
_REMOTE_JOB_STATES = {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}


def _lease_token_digest(token: str) -> str:
    """Hash high-entropy worker lease tokens before persistent storage."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _idempotency_key_digest(
    *, tenant_id: str | None, project_id: str, provider_id: str, key: str
) -> str:
    """Scope a client key to one tenant/project/provider without storing the key."""
    material = json.dumps(
        ["provider-job-idempotency-v1", tenant_id, project_id, provider_id, key],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProviderJobManager:
    """Queue references only; hash worker leases and never persist raw payloads."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_queue_size: int = 100,
        max_concurrent: int = 4,
        max_job_records: int = 100_000,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        backpressure_retry_seconds: int = 5,
        scheduling_strategy: str = "PROJECT_ROUND_ROBIN",
        clock: Any = None,
    ) -> None:
        for value, name, minimum, maximum in (
            (max_queue_size, "max_queue_size", 1, 100000),
            (max_job_records, "max_job_records", 1, 1_000_000),
            (max_concurrent, "max_concurrent", 1, 10000),
            (lease_seconds, "lease_seconds", 1, 86400),
            (max_attempts, "max_attempts", 1, 20),
            (backpressure_retry_seconds, "backpressure_retry_seconds", 1, 3600),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside safe limits")
        if scheduling_strategy not in {"PROJECT_ROUND_ROBIN", "GLOBAL_PRIORITY"}:
            raise ValueError("scheduling_strategy must be PROJECT_ROUND_ROBIN or GLOBAL_PRIORITY")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.scheduling_strategy = scheduling_strategy
        self.state_path = Path(state_path)
        self.max_queue_size = max_queue_size
        self.max_job_records = max_job_records
        self.max_concurrent = max_concurrent
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.backpressure_retry_seconds = backpressure_retry_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": "1.0", "jobs": {}, "idempotency_index": {},
            "scheduler": {"last_project_id": None},
        }
        if self.state_path.exists():
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                if self._reload():
                    self._save()
        else:
            self._reload()

    def _reload(self) -> bool:
        if not self.state_path.exists():
            self.data = {
                "version": "1.0", "jobs": {}, "idempotency_index": {},
                "scheduler": {"last_project_id": None},
            }
            return False
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider job state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("jobs"), dict)
            or len(loaded["jobs"]) > self.max_job_records
        ):
            raise ValueError("invalid provider job state schema or capacity")
        scheduler = loaded.get("scheduler", {"last_project_id": None})
        if (
            not isinstance(scheduler, dict)
            or set(scheduler) != {"last_project_id"}
            or (
                scheduler["last_project_id"] is not None
                and (
                    not isinstance(scheduler["last_project_id"], str)
                    or not _ID_RE.fullmatch(scheduler["last_project_id"])
                )
            )
        ):
            raise ValueError("invalid provider job scheduler state")
        loaded["scheduler"] = scheduler
        index_missing = "idempotency_index" not in loaded
        idempotency_index = loaded.get("idempotency_index", {})
        if not isinstance(idempotency_index, dict) or len(idempotency_index) > self.max_job_records:
            raise ValueError("invalid provider job idempotency index")
        migrated_legacy_tokens = False
        expected_idempotency_index: dict[str, str] = {}
        for job_id, job in loaded["jobs"].items():
            if isinstance(job, dict) and "lease_token" in job and "lease_token_hash" not in job:
                legacy_token = job.pop("lease_token")
                if legacy_token is not None and (
                    not isinstance(legacy_token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", legacy_token)
                ):
                    raise ValueError("invalid provider job record")
                job["lease_token_hash"] = (
                    _lease_token_digest(legacy_token) if legacy_token is not None else None
                )
                migrated_legacy_tokens = True
            if (
                not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
                or not isinstance(job, dict)
                or job.get("job_id") != job_id
                or job.get("status") not in {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}
                or not isinstance(job.get("project_id"), str)
                or not isinstance(job.get("provider_id"), str)
                or not isinstance(job.get("payload_ref"), str)
                or not isinstance(job.get("worker_id"), (str, type(None)))
                or (
                    job.get("worker_id") is not None
                    and not _ID_RE.fullmatch(job["worker_id"])
                )
                or ("lease_token" in job and "lease_token_hash" in job)
                or (
                    "lease_token" in job
                    and job.get("lease_token") is not None
                    and not isinstance(job.get("lease_token"), str)
                )
                or (
                    "lease_token" in job
                    and job.get("lease_token") is not None
                    and not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", job["lease_token"])
                )
                or (
                    "lease_token_hash" in job
                    and job.get("lease_token_hash") is not None
                    and (
                        not isinstance(job.get("lease_token_hash"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", job["lease_token_hash"])
                    )
                )
                or (
                    job.get("status") == "RUNNING"
                    and (
                        job.get("worker_id") is None
                        or (
                            job.get("lease_token") is None
                            and job.get("lease_token_hash") is None
                        )
                    )
                )
                or (
                    job.get("status") != "RUNNING"
                    and (
                        job.get("lease_token") is not None
                        or job.get("lease_token_hash") is not None
                    )
                )
                or (
                    job.get("tenant_id") is not None
                    and (
                        not isinstance(job.get("tenant_id"), str)
                        or not _ID_RE.fullmatch(job.get("tenant_id"))
                    )
                )
                or isinstance(job.get("attempts"), bool)
                or not isinstance(job.get("attempts"), int) or job["attempts"] < 0
                or isinstance(job.get("priority"), bool)
                or not isinstance(job.get("priority"), int) or not 0 <= job["priority"] <= 100
                or not isinstance(job.get("remote_submission_state", "NOT_SUBMITTED"), str)
                or job.get("remote_submission_state", "NOT_SUBMITTED") not in _REMOTE_SUBMISSION_STATES
                or (
                    job.get("remote_job_id") is not None
                    and (
                        not isinstance(job.get("remote_job_id"), str)
                        or not _REF_RE.fullmatch(job["remote_job_id"])
                    )
                )
                or (
                    job.get("remote_status") is not None
                    and (
                        not isinstance(job.get("remote_status"), str)
                        or job.get("remote_status") not in _REMOTE_JOB_STATES
                    )
                )
                or (
                    job.get("remote_poll_after_seconds") is not None
                    and (
                        isinstance(job.get("remote_poll_after_seconds"), bool)
                        or not isinstance(job.get("remote_poll_after_seconds"), int)
                        or not 1 <= job["remote_poll_after_seconds"] <= 3600
                    )
                )
                or (
                    job.get("remote_submission_attempts", 0) is None
                    or isinstance(job.get("remote_submission_attempts", 0), bool)
                    or not isinstance(job.get("remote_submission_attempts", 0), int)
                    or not 0 <= job.get("remote_submission_attempts", 0) <= 1_000_000
                )
                or (
                    job.get("remote_submission_worker_id") is not None
                    and (
                        not isinstance(job.get("remote_submission_worker_id"), str)
                        or not _ID_RE.fullmatch(job["remote_submission_worker_id"])
                    )
                )
                or (
                    job.get("remote_submission_lease_token_hash") is not None
                    and (
                        not isinstance(job.get("remote_submission_lease_token_hash"), str)
                        or not _SHA256_RE.fullmatch(job["remote_submission_lease_token_hash"])
                    )
                )
                or (
                    job.get("remote_submission_state", "NOT_SUBMITTED") == "SUBMITTING"
                    and (
                        job.get("remote_submission_worker_id") is None
                        or job.get("remote_submission_lease_token_hash") is None
                        or job.get("remote_job_id") is not None
                        or job.get("remote_status") is not None
                    )
                )
                or (
                    job.get("remote_submission_state", "NOT_SUBMITTED") == "UNKNOWN"
                    and (
                        job.get("remote_submission_worker_id") is not None
                        or job.get("remote_submission_lease_token_hash") is not None
                        or job.get("remote_job_id") is not None
                        or job.get("remote_status") is not None
                    )
                )
                or (
                    job.get("remote_submission_state", "NOT_SUBMITTED") == "SUBMITTED"
                    and (
                        job.get("remote_submission_worker_id") is not None
                        or job.get("remote_submission_lease_token_hash") is not None
                        or job.get("remote_job_id") is None
                        or not isinstance(job.get("remote_status"), str)
                        or job.get("remote_status") not in _REMOTE_JOB_STATES
                        or (
                            job.get("remote_status") in {"QUEUED", "RUNNING"}
                            and job.get("remote_poll_after_seconds") is None
                        )
                        or (
                            job.get("remote_status") in _TERMINAL
                            and job.get("remote_poll_after_seconds") is not None
                        )
                    )
                )
                or (
                    job.get("remote_submission_state", "NOT_SUBMITTED") == "NOT_SUBMITTED"
                    and (
                        job.get("remote_submission_worker_id") is not None
                        or job.get("remote_submission_lease_token_hash") is not None
                        or job.get("remote_job_id") is not None
                        or job.get("remote_status") is not None
                    )
                )
            ):
                raise ValueError("invalid provider job record")
            key_digest = job.get("idempotency_key_hash")
            fingerprint = job.get("request_fingerprint")
            if key_digest is None:
                if fingerprint is not None:
                    raise ValueError("invalid provider job idempotency record")
            else:
                if (
                    not isinstance(key_digest, str) or not _SHA256_RE.fullmatch(key_digest)
                    or not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint)
                    or key_digest in expected_idempotency_index
                ):
                    raise ValueError("invalid provider job idempotency record")
                expected_idempotency_index[key_digest] = job_id
        if not index_missing and idempotency_index != expected_idempotency_index:
            raise ValueError("provider job idempotency index does not match stored jobs")
        loaded["idempotency_index"] = expected_idempotency_index
        self.data = loaded
        return migrated_legacy_tokens or index_missing

    @staticmethod
    def _public_job(
        job: dict[str, Any],
        *,
        lease_token: str | None = None,
        include_provider_idempotency_key: bool = False,
    ) -> dict[str, Any]:
        """Hide internal digests; expose a stable provider key only to a claiming worker."""
        public_job = dict(job)
        public_job.pop("lease_token_hash", None)
        public_job.pop("lease_token", None)
        key_digest = public_job.pop("idempotency_key_hash", None)
        public_job.pop("request_fingerprint", None)
        if lease_token is not None:
            public_job["lease_token"] = lease_token
        if include_provider_idempotency_key and isinstance(key_digest, str):
            public_job["provider_idempotency_key"] = key_digest
        return public_job

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                migrated = self._reload()
                if migrated:
                    self._save()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
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
        tenant_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        for value, name in ((job_id, "job_id"), (project_id, "project_id"), (provider_id, "provider_id")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "accepted": False}
        if not isinstance(payload_ref, str) or not _REF_RE.fullmatch(payload_ref):
            return {"status": "INVALID_PAYLOAD_REFERENCE", "accepted": False}
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not _ID_RE.fullmatch(tenant_id)
        ):
            return {"status": "INVALID_TENANT_ID", "accepted": False}
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            return {"status": "INVALID_PRIORITY", "accepted": False}
        request_fingerprint = None
        if idempotency_key is None:
            key_digest = None
        else:
            if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
                return {"status": "INVALID_IDEMPOTENCY_KEY", "accepted": False}
            key_digest = _idempotency_key_digest(
                tenant_id=tenant_id, project_id=project_id,
                provider_id=provider_id, key=idempotency_key,
            )
            fingerprint_material = json.dumps(
                ["provider-job-request-v1", tenant_id, project_id, provider_id, payload_ref, priority],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            request_fingerprint = hashlib.sha256(
                fingerprint_material.encode("utf-8")
            ).hexdigest()
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            if key_digest is not None:
                prior_job_id = self.data["idempotency_index"].get(key_digest)
                if prior_job_id is not None:
                    prior = (
                        self.data["jobs"].get(prior_job_id)
                        if isinstance(prior_job_id, str) else None
                    )
                    if not isinstance(prior, dict):
                        return {"status": "IDEMPOTENCY_STATE_INVALID", "accepted": False}
                    same_scope = all(prior.get(key) == value for key, value in {
                        "tenant_id": tenant_id, "project_id": project_id,
                        "provider_id": provider_id,
                    }.items())
                    if not same_scope:
                        return {"status": "IDEMPOTENCY_STATE_INVALID", "accepted": False}
                    if prior.get("request_fingerprint") != request_fingerprint:
                        return {"status": "IDEMPOTENCY_KEY_CONFLICT", "accepted": False}
                    return {
                        "status": "ALREADY_SUBMITTED", "accepted": True,
                        "idempotency_replayed": True,
                        "job": self._public_job(prior),
                    }
            existing = self.data["jobs"].get(job_id)
            if isinstance(existing, dict):
                if existing.get("tenant_id") != tenant_id:
                    return {"status": "TENANT_ISOLATION_DENIED", "accepted": False}
                same = all(existing.get(key) == value for key, value in {
                    "project_id": project_id, "provider_id": provider_id,
                    "payload_ref": payload_ref, "priority": priority,
                }.items())
                same_idempotency = (
                    key_digest is None and existing.get("idempotency_key_hash") is None
                ) or (
                    key_digest is not None
                    and existing.get("idempotency_key_hash") == key_digest
                    and existing.get("request_fingerprint") == request_fingerprint
                )
                same = same and same_idempotency
                return {
                    "status": "ALREADY_SUBMITTED" if same else "REQUEST_ID_CONFLICT",
                    "accepted": same,
                    "job": self._public_job(existing) if same else None,
                }
            if len(self.data["jobs"]) >= self.max_job_records:
                return {"status": "JOB_HISTORY_CAPACITY_REACHED", "accepted": False}
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
            if tenant_id is not None:
                job["tenant_id"] = tenant_id
            if key_digest is not None:
                job["idempotency_key_hash"] = key_digest
                job["request_fingerprint"] = request_fingerprint
                self.data["idempotency_index"][key_digest] = job_id
            self.data["jobs"][job_id] = job
            self._save()
            return {"status": "JOB_QUEUED", "accepted": True, "job": self._public_job(job)}

    def _reap_expired_locked(self, now: datetime) -> bool:
        changed = False
        for job in self.data["jobs"].values():
            if not isinstance(job, dict) or job.get("status") != "RUNNING":
                continue
            lease_until = job.get("lease_until")
            try:
                if not isinstance(lease_until, str):
                    expired = True
                else:
                    parsed_lease = datetime.fromisoformat(lease_until)
                    if parsed_lease.tzinfo is None:
                        expired = True
                    else:
                        expired = parsed_lease.astimezone(timezone.utc) <= now
            except (ValueError, TypeError, OverflowError):
                expired = True
            if not expired:
                continue
            job["updated_at"] = now.isoformat()
            job["worker_id"] = None
            job["lease_token_hash"] = None
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
        now = self.clock().astimezone(timezone.utc)
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
            if not queued:
                if changed:
                    self._save()
                return {"status": "QUEUE_EMPTY", "job": None}

            if self.scheduling_strategy == "GLOBAL_PRIORITY":
                queued.sort(key=lambda job: (-job["priority"], job["created_at"], job["job_id"]))
                job = queued[0]
            else:
                project_jobs: dict[str, list[dict[str, Any]]] = {}
                for candidate in queued:
                    project_jobs.setdefault(candidate["project_id"], []).append(candidate)
                project_order = sorted(
                    project_jobs,
                    key=lambda project_id: (
                        min(item["created_at"] for item in project_jobs[project_id]),
                        project_id,
                    ),
                )
                last_project = self.data["scheduler"]["last_project_id"]
                if last_project in project_order:
                    selected_index = (project_order.index(last_project) + 1) % len(project_order)
                    selected_project = project_order[selected_index]
                else:
                    selected_project = project_order[0]
                candidates = project_jobs[selected_project]
                candidates.sort(
                    key=lambda item: (-item["priority"], item["created_at"], item["job_id"])
                )
                job = candidates[0]
                self.data["scheduler"]["last_project_id"] = selected_project
            job["status"] = "RUNNING"
            job["worker_id"] = worker_id
            lease_token = secrets.token_urlsafe(32)
            job["lease_token_hash"] = _lease_token_digest(lease_token)
            job.pop("lease_token", None)
            job["attempts"] += 1
            job["started_at"] = job.get("started_at") or now.isoformat()
            job["updated_at"] = now.isoformat()
            job["lease_until"] = (now + timedelta(seconds=self.lease_seconds)).isoformat()
            self._save()
            return {
                "status": "JOB_CLAIMED",
                "job": self._public_job(
                    job, lease_token=lease_token,
                    include_provider_idempotency_key=True,
                ),
            }

    def renew_lease(
        self, *, job_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "renewed": False}
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "renewed": False}
        if not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token):
            return {"status": "INVALID_LEASE_TOKEN", "renewed": False}
        now = self.clock().astimezone(timezone.utc)
        with self._transaction():
            changed = self._reap_expired_locked(now)
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                if changed:
                    self._save()
                return {"status": "JOB_NOT_FOUND", "renewed": False}
            if (
                job.get("status") != "RUNNING"
                or job.get("worker_id") != worker_id
                or not secrets.compare_digest(str(job.get("lease_token_hash", "")), _lease_token_digest(lease_token))
            ):
                if changed:
                    self._save()
                return {"status": "JOB_LEASE_NOT_OWNED", "renewed": False}
            job["lease_until"] = (
                now + timedelta(seconds=self.lease_seconds)
            ).isoformat()
            job["updated_at"] = now.isoformat()
            self._save()
            return {
                "status": "JOB_LEASE_RENEWED",
                "renewed": True,
                "lease_until": job["lease_until"],
                "job": self._public_job(job),
            }

    def complete(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        success: bool,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "saved": False}
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "saved": False}
        if not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token):
            return {"status": "INVALID_LEASE_TOKEN", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        if success and (not isinstance(result_ref, str) or not _REF_RE.fullmatch(result_ref)):
            return {"status": "INVALID_RESULT_REFERENCE", "saved": False}
        if not success and error_code is not None and (
            not isinstance(error_code, str) or not _ERROR_RE.fullmatch(error_code)
        ):
            return {"status": "INVALID_ERROR_CODE", "saved": False}
        now_dt = self.clock().astimezone(timezone.utc)
        now = now_dt.isoformat()
        with self._transaction():
            changed = self._reap_expired_locked(now_dt)
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                if changed:
                    self._save()
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if changed:
                self._save()
            if (
                job.get("status") != "RUNNING"
                or job.get("worker_id") != worker_id
                or not secrets.compare_digest(str(job.get("lease_token_hash", "")), _lease_token_digest(lease_token))
            ):
                return {"status": "JOB_LEASE_NOT_OWNED", "saved": False}
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
            job["lease_token_hash"] = None
            job["worker_id"] = None
            self._save()
            return {"status": "JOB_COMPLETED", "saved": True, "job": self._public_job(job)}

    def cancel(
        self, *, job_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "saved": False}
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not _ID_RE.fullmatch(tenant_id)
        ):
            return {"status": "INVALID_TENANT_ID", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if job.get("tenant_id") != tenant_id:
                return {"status": "TENANT_ISOLATION_DENIED", "saved": False}
            if job.get("status") == "QUEUED":
                job["status"] = "CANCELLED"
                job["completed_at"] = now
            elif job.get("status") == "RUNNING":
                job["cancel_requested"] = True
            else:
                return {"status": "JOB_TERMINAL", "saved": False}
            job["updated_at"] = now
            self._save()
            return {"status": "CANCEL_REQUESTED" if job["status"] == "RUNNING" else "JOB_CANCELLED", "saved": True, "job": self._public_job(job)}


    @staticmethod
    def _lease_matches(job: dict[str, Any], worker_id: str, lease_token: str) -> bool:
        return (
            job.get("status") == "RUNNING"
            and job.get("worker_id") == worker_id
            and isinstance(job.get("lease_token_hash"), str)
            and secrets.compare_digest(
                job["lease_token_hash"], _lease_token_digest(lease_token)
            )
        )

    @staticmethod
    def _clear_worker_lease(job: dict[str, Any]) -> None:
        job["worker_id"] = None
        job["lease_token_hash"] = None
        job["lease_until"] = None

    def get_worker_job(
        self, *, job_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        """Return worker-only context after verifying the live lease capability."""
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "job": None}
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "job": None}
        if not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token):
            return {"status": "INVALID_LEASE_TOKEN", "job": None}
        now = self.clock().astimezone(timezone.utc)
        with self._transaction():
            changed = self._reap_expired_locked(now)
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                if changed:
                    self._save()
                return {"status": "JOB_NOT_FOUND", "job": None}
            if not self._lease_matches(job, worker_id, lease_token):
                if changed:
                    self._save()
                return {"status": "JOB_LEASE_NOT_OWNED", "job": None}
            return {
                "status": "JOB_WORKER_CONTEXT",
                "job": self._public_job(job, include_provider_idempotency_key=True),
            }

    def begin_remote_submission(
        self, *, job_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        """Persist intent before calling a provider; retries require the same hashed key."""
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "saved": False}
        if not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id):
            return {"status": "INVALID_WORKER_ID", "saved": False}
        if not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token):
            return {"status": "INVALID_LEASE_TOKEN", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if not self._lease_matches(job, worker_id, lease_token):
                return {"status": "JOB_LEASE_NOT_OWNED", "saved": False}
            if not isinstance(job.get("idempotency_key_hash"), str):
                return {"status": "REMOTE_PROVIDER_IDEMPOTENCY_REQUIRED", "saved": False}
            state = job.get("remote_submission_state", "NOT_SUBMITTED")
            if state == "SUBMITTED":
                return {
                    "status": "REMOTE_JOB_ALREADY_SUBMITTED",
                    "saved": False,
                    "remote_job_id": job.get("remote_job_id"),
                    "remote_status": job.get("remote_status"),
                }
            current_lease_hash = _lease_token_digest(lease_token)
            if (
                state == "SUBMITTING"
                and job.get("remote_submission_lease_token_hash") == current_lease_hash
            ):
                # This same lease already issued the remote call. Never issue it twice.
                return {"status": "REMOTE_SUBMISSION_IN_PROGRESS", "saved": False}
            # A reclaimed worker lease can safely retry with the same provider idempotency digest.
            job["remote_submission_state"] = "SUBMITTING"
            job["remote_submission_worker_id"] = worker_id
            job["remote_submission_lease_token_hash"] = current_lease_hash
            job["remote_submission_attempts"] = job.get("remote_submission_attempts", 0) + 1
            job["remote_updated_at"] = now
            job.pop("remote_error_code", None)
            job["updated_at"] = now
            self._save()
            return {
                "status": "REMOTE_SUBMISSION_STARTED",
                "saved": True,
                "attempt": job["remote_submission_attempts"],
            }

    def record_remote_submission_unknown(
        self, *, job_id: str, worker_id: str, lease_token: str,
        error_code: str = "REMOTE_SUBMISSION_UNKNOWN",
    ) -> dict[str, Any]:
        if (
            not isinstance(error_code, str)
            or not _ERROR_RE.fullmatch(error_code)
            or not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
            or not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id)
            or not isinstance(lease_token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token)
        ):
            return {"status": "INVALID_REMOTE_SUBMISSION_STATE", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if not self._lease_matches(job, worker_id, lease_token):
                return {"status": "JOB_LEASE_NOT_OWNED", "saved": False}
            if job.get("remote_submission_state") != "SUBMITTING":
                return {"status": "REMOTE_SUBMISSION_NOT_IN_PROGRESS", "saved": False}
            job["remote_submission_state"] = "UNKNOWN"
            job["remote_submission_worker_id"] = None
            job["remote_submission_lease_token_hash"] = None
            job["remote_error_code"] = error_code
            job["remote_updated_at"] = now
            job["updated_at"] = now
            self._save()
            return {"status": "REMOTE_SUBMISSION_UNKNOWN", "saved": True}

    def record_remote_submission(
        self, *, job_id: str, worker_id: str, lease_token: str,
        remote_job_id: str, remote_status: str,
        poll_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(remote_job_id, str) or not _REF_RE.fullmatch(remote_job_id)
            or remote_status not in _REMOTE_JOB_STATES
            or (
                remote_status in {"QUEUED", "RUNNING"}
                and (
                    isinstance(poll_after_seconds, bool)
                    or not isinstance(poll_after_seconds, int)
                    or not 1 <= poll_after_seconds <= 3600
                )
            )
            or (
                remote_status in _TERMINAL
                and poll_after_seconds is not None
                and (
                    isinstance(poll_after_seconds, bool)
                    or not isinstance(poll_after_seconds, int)
                    or not 1 <= poll_after_seconds <= 3600
                )
            )
            or not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
            or not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id)
            or not isinstance(lease_token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token)
        ):
            return {"status": "INVALID_REMOTE_JOB_RESPONSE", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if not self._lease_matches(job, worker_id, lease_token):
                return {"status": "JOB_LEASE_NOT_OWNED", "saved": False}
            state = job.get("remote_submission_state", "NOT_SUBMITTED")
            if state == "SUBMITTED":
                same = job.get("remote_job_id") == remote_job_id
                return {
                    "status": "REMOTE_JOB_ALREADY_SUBMITTED" if same else "REMOTE_JOB_ID_CONFLICT",
                    "saved": same,
                    "remote_job_id": job.get("remote_job_id"),
                }
            if state != "SUBMITTING":
                return {"status": "REMOTE_SUBMISSION_NOT_IN_PROGRESS", "saved": False}
            job["remote_submission_state"] = "SUBMITTED"
            job["remote_submission_worker_id"] = None
            job["remote_submission_lease_token_hash"] = None
            job["remote_job_id"] = remote_job_id
            job["remote_status"] = remote_status
            job["remote_poll_after_seconds"] = (
                poll_after_seconds if remote_status in {"QUEUED", "RUNNING"} else None
            )
            job["remote_error_code"] = None
            job["remote_updated_at"] = now
            job["updated_at"] = now
            self._save()
            return {
                "status": "REMOTE_JOB_SUBMITTED",
                "saved": True,
                "remote_job_id": remote_job_id,
                "remote_status": remote_status,
                "poll_after_seconds": job["remote_poll_after_seconds"],
            }

    def update_remote_status(
        self, *, job_id: str, worker_id: str, lease_token: str,
        remote_job_id: str, remote_status: str,
        poll_after_seconds: int | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(remote_job_id, str) or not _REF_RE.fullmatch(remote_job_id)
            or remote_status not in _REMOTE_JOB_STATES
            or not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
            or not isinstance(worker_id, str) or not _ID_RE.fullmatch(worker_id)
            or not isinstance(lease_token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", lease_token)
            or (
                remote_status in {"QUEUED", "RUNNING"}
                and (
                    isinstance(poll_after_seconds, bool)
                    or not isinstance(poll_after_seconds, int)
                    or not 1 <= poll_after_seconds <= 3600
                )
            )
            or (
                remote_status == "SUCCEEDED"
                and (not isinstance(result_ref, str) or not _REF_RE.fullmatch(result_ref))
            )
            or (
                remote_status == "FAILED"
                and (
                    not isinstance(error_code, str)
                    or not _ERROR_RE.fullmatch(error_code)
                )
            )
            or (result_ref is not None and (
                not isinstance(result_ref, str) or not _REF_RE.fullmatch(result_ref)
            ))
            or (error_code is not None and (
                not isinstance(error_code, str) or not _ERROR_RE.fullmatch(error_code)
            ))
        ):
            return {"status": "INVALID_REMOTE_JOB_STATUS", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if not self._lease_matches(job, worker_id, lease_token):
                return {"status": "JOB_LEASE_NOT_OWNED", "saved": False}
            if (
                job.get("remote_submission_state") != "SUBMITTED"
                or job.get("remote_job_id") != remote_job_id
            ):
                return {"status": "REMOTE_JOB_IDENTITY_MISMATCH", "saved": False}
            job["remote_status"] = remote_status
            job["remote_poll_after_seconds"] = (
                poll_after_seconds if remote_status in {"QUEUED", "RUNNING"} else None
            )
            job["remote_error_code"] = error_code if remote_status == "FAILED" else None
            job["remote_updated_at"] = now
            if remote_status in _TERMINAL:
                job["status"] = remote_status
                if remote_status == "SUCCEEDED":
                    job["result_ref"] = result_ref
                elif remote_status == "FAILED":
                    job["error_code"] = error_code
                job["completed_at"] = now
                self._clear_worker_lease(job)
            job["updated_at"] = now
            self._save()
            return {
                "status": "REMOTE_JOB_TERMINAL" if remote_status in _TERMINAL else "REMOTE_JOB_PENDING",
                "saved": True,
                "terminal": remote_status in _TERMINAL,
                "job": self._public_job(job),
            }

    def confirm_remote_cancellation(
        self, *, job_id: str, remote_job_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        if (
            not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id)
            or not isinstance(remote_job_id, str) or not _REF_RE.fullmatch(remote_job_id)
            or (tenant_id is not None and (
                not isinstance(tenant_id, str) or not _ID_RE.fullmatch(tenant_id)
            ))
        ):
            return {"status": "INVALID_REMOTE_CANCELLATION", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "saved": False}
            if job.get("tenant_id") != tenant_id:
                return {"status": "TENANT_ISOLATION_DENIED", "saved": False}
            if (
                job.get("remote_submission_state") != "SUBMITTED"
                or job.get("remote_job_id") != remote_job_id
            ):
                return {"status": "REMOTE_JOB_IDENTITY_MISMATCH", "saved": False}
            if job.get("status") in _TERMINAL:
                return {"status": "JOB_TERMINAL", "saved": False}
            job["status"] = "CANCELLED"
            job["remote_status"] = "CANCELLED"
            job["remote_poll_after_seconds"] = None
            job["cancel_requested"] = False
            job["completed_at"] = now
            job["remote_updated_at"] = now
            job["updated_at"] = now
            self._clear_worker_lease(job)
            self._save()
            return {"status": "REMOTE_JOB_CANCELLED", "saved": True, "job": self._public_job(job)}

    def poll(
        self, *, job_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(job_id, str) or not _ID_RE.fullmatch(job_id):
            return {"status": "INVALID_JOB_ID", "job": None}
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not _ID_RE.fullmatch(tenant_id)
        ):
            return {"status": "INVALID_TENANT_ID", "job": None}
        with self._transaction():
            job = self.data["jobs"].get(job_id)
            if not isinstance(job, dict):
                return {"status": "JOB_NOT_FOUND", "job": None}
            if job.get("tenant_id") != tenant_id:
                return {"status": "TENANT_ISOLATION_DENIED", "job": None}
            return {"status": "JOB_STATUS", "job": self._public_job(job)}
