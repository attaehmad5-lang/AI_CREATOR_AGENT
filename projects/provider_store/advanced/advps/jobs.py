"""Persistent generation-job lifecycle, idempotency, leases, cancellation and crash recovery."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Optional

from .errors import NotFoundError, ValidationError
from .storage import AtomicJsonStore
from .redact import redact_text

STATUSES = {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}


def _empty() -> dict:
    return {"schema": 1, "jobs": {}, "idempotency": {}}


def _payload_hash(payload: Any) -> str:
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError("payload must be finite JSON data") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _key_hash(key: Optional[str]) -> Optional[str]:
    if key is None:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _has_sensitive_metadata(value: Any) -> bool:
    forbidden = ("token", "secret", "password", "credential", "authorization", "api_key", "cookie", "private_key")
    if isinstance(value, dict):
        return any(any(marker in str(key).casefold().replace("-", "_") for marker in forbidden)
                   or _has_sensitive_metadata(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_sensitive_metadata(item) for item in value)
    if isinstance(value, str):
        return redact_text(value) != value
    return False


class JobManager:
    """Stores job metadata only; never stores payloads, credentials, or provider responses."""

    def __init__(self, store: AtomicJsonStore, clock: Callable[[], float] = time.time):
        self.store = store
        self.store.default = _empty
        self.clock = clock

    def create(
        self,
        provider_id: str,
        capability: str,
        payload: Any,
        *,
        idempotency_key: Optional[str] = None,
        max_attempts: int = 3,
        metadata: Optional[dict] = None,
    ) -> dict:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValidationError("provider_id must be non-empty")
        if not isinstance(capability, str) or not capability.strip():
            raise ValidationError("capability must be non-empty")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValidationError("max_attempts must be a positive integer")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValidationError("idempotency_key must be non-empty")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be an object")
        # Metadata is intentionally restricted to non-sensitive primitive fields.
        metadata = metadata or {}
        if _has_sensitive_metadata(metadata):
            raise ValidationError("secret-bearing job metadata is forbidden")
        digest = _payload_hash(payload)
        key_hash = _key_hash(idempotency_key)
        result = {}

        def op(state):
            if key_hash and key_hash in state["idempotency"]:
                existing = state["jobs"][state["idempotency"][key_hash]]
                if (existing["payload_hash"] != digest or existing["provider_id"] != provider_id
                        or existing["capability"] != capability):
                    raise ValidationError("idempotency key reused with different request")
                result.update(existing)
                result["duplicate"] = True
                return None
            job_id = uuid.uuid4().hex
            now = self.clock()
            job = {
                "id": job_id, "provider_id": provider_id, "capability": capability,
                "payload_hash": digest, "status": "PENDING", "created_at": now, "updated_at": now,
                "attempts": 0, "max_attempts": max_attempts, "lease_owner": None,
                "lease_expires_at": None, "result_ref": None, "error_code": None,
                "idempotency_key_hash": key_hash, "metadata": dict(metadata),
            }
            state["jobs"][job_id] = job
            if key_hash:
                state["idempotency"][key_hash] = job_id
            result.update(job)
            result["duplicate"] = False
            return None

        self.store.update(op)
        return result

    def get(self, job_id: str) -> dict:
        job = self.store.read()["jobs"].get(job_id)
        if job is None:
            raise NotFoundError(f"unknown job: {job_id}")
        return job

    def list(self, status: Optional[str] = None) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValidationError(f"invalid status: {status}")
        jobs = self.store.read()["jobs"].values()
        return sorted((j for j in jobs if status is None or j["status"] == status),
                      key=lambda j: (j["created_at"], j["id"]))

    def claim(self, job_id: str, worker_id: str, lease_seconds: float = 60.0) -> dict:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValidationError("worker_id must be non-empty")
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise ValidationError("lease_seconds must be finite and > 0")
        result = {}

        def op(state):
            job = state["jobs"].get(job_id)
            if job is None:
                raise NotFoundError(f"unknown job: {job_id}")
            now = self.clock()
            if job["status"] == "RUNNING" and job["lease_expires_at"] is not None and job["lease_expires_at"] > now:
                raise ValidationError("job already has an active lease")
            if job["status"] not in {"PENDING", "RUNNING"}:
                raise ValidationError(f"cannot claim job in {job['status']} state")
            if job["attempts"] >= job["max_attempts"]:
                job["status"] = "FAILED"
                job["error_code"] = "max_attempts_exhausted"
                job["updated_at"] = now
                result.update(job)
                return None
            job["status"] = "RUNNING"
            job["attempts"] += 1
            job["lease_owner"] = worker_id
            job["lease_expires_at"] = now + float(lease_seconds)
            job["updated_at"] = now
            result.update(job)
        self.store.update(op)
        return result

    def complete(self, job_id: str, worker_id: str, result_ref: str) -> dict:
        if not isinstance(result_ref, str) or not result_ref.strip():
            raise ValidationError("result_ref must be non-empty")
        result = {}

        def op(state):
            job = state["jobs"].get(job_id)
            if job is None:
                raise NotFoundError(f"unknown job: {job_id}")
            now = self.clock()
            if job["status"] != "RUNNING" or job["lease_owner"] != worker_id:
                raise ValidationError("worker does not own the running job")
            if job["lease_expires_at"] is None or job["lease_expires_at"] <= now:
                raise ValidationError("job lease expired")
            job.update(status="SUCCEEDED", result_ref=result_ref, error_code=None,
                       lease_owner=None, lease_expires_at=None, updated_at=now)
            result.update(job)
        self.store.update(op)
        return result

    def fail(self, job_id: str, worker_id: str, error_code: str, retryable: bool = False) -> dict:
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", error_code):
            raise ValidationError("error_code must be a short identifier, not an error message")
        # Persist a code, never provider exception text that may contain secrets.
        result = {}

        def op(state):
            job = state["jobs"].get(job_id)
            if job is None:
                raise NotFoundError(f"unknown job: {job_id}")
            if job["status"] != "RUNNING" or job["lease_owner"] != worker_id:
                raise ValidationError("worker does not own the running job")
            now = self.clock()
            can_retry = retryable and job["attempts"] < job["max_attempts"]
            job.update(status="PENDING" if can_retry else "FAILED",
                       error_code=error_code[:80], lease_owner=None,
                       lease_expires_at=None, updated_at=now)
            result.update(job)
        self.store.update(op)
        return result

    def cancel(self, job_id: str) -> dict:
        result = {}

        def op(state):
            job = state["jobs"].get(job_id)
            if job is None:
                raise NotFoundError(f"unknown job: {job_id}")
            if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise ValidationError(f"cannot cancel terminal job in {job['status']} state")
            job.update(status="CANCELLED", lease_owner=None, lease_expires_at=None, updated_at=self.clock())
            result.update(job)
        self.store.update(op)
        return result

    def recover_stale(self) -> dict:
        """Return expired RUNNING leases to PENDING or FAILED when attempts are exhausted."""
        result = {"requeued": 0, "failed": 0}
        now = self.clock()

        def op(state):
            for job in state["jobs"].values():
                if job["status"] != "RUNNING" or job["lease_expires_at"] is None or job["lease_expires_at"] > now:
                    continue
                job["lease_owner"] = None
                job["lease_expires_at"] = None
                job["updated_at"] = now
                if job["attempts"] < job["max_attempts"]:
                    job["status"] = "PENDING"
                    job["error_code"] = "lease_expired"
                    result["requeued"] += 1
                else:
                    job["status"] = "FAILED"
                    job["error_code"] = "lease_expired_max_attempts"
                    result["failed"] += 1
        self.store.update(op)
        return result
