"""Durable request-idempotency state for provider execution attempts."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_STATES = {"RESERVED", "STARTED", "COMPLETED", "NOT_STARTED", "UNKNOWN"}
_OUTCOMES = {"completed": "COMPLETED", "not_started": "NOT_STARTED", "unknown": "UNKNOWN"}
_REQUEST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class ProviderRequestIdempotencyManager:
    """Serialize execution claims and prevent duplicate adapter invocations.

    STARTED/UNKNOWN records are never automatically reclaimed: after an adapter
    may have transmitted a paid request, retrying can create a second charge.
    Only expired RESERVED and explicit NOT_STARTED records may be retried.
    """

    def __init__(
        self, path: str | os.PathLike[str], *,
        lease_seconds: int = 300, max_records: int = 100_000,
        clock: Any = time.time,
    ) -> None:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be between 1 and 1000000")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.max_records = max_records
        self._clock = clock
        self._lock = threading.RLock()
        if not self.path.exists():
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                if not self.path.exists():
                    self._save({"version": 1, "requests": {}})
        self._load()

    @staticmethod
    def _key(tenant_id: str | None, project_id: str, request_id: str) -> str:
        raw = json.dumps(
            [tenant_id or "", project_id, request_id],
            separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _valid_id(value: Any, *, optional: bool = False) -> bool:
        if value is None and optional:
            return True
        return isinstance(value, str) and _ID_RE.fullmatch(value) is not None

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
            raise ValueError("invalid idempotency clock")
        return float(value)

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("idempotency state is unavailable or malformed") from None
        if not isinstance(state, dict) or set(state) != {"version", "requests"} or state.get("version") != 1:
            raise ValueError("idempotency state schema is invalid")
        requests = state.get("requests")
        if not isinstance(requests, dict) or len(requests) > self.max_records:
            raise ValueError("idempotency request collection is invalid")
        for key, record in requests.items():
            if not isinstance(key, str) or _HASH_RE.fullmatch(key) is None:
                raise ValueError("idempotency key is invalid")
            if not isinstance(record, dict) or set(record) != {
                "tenant_id", "project_id", "request_id", "fingerprint", "state",
                "lease_token", "lease_expires_at", "attempts", "created_at",
                "updated_at", "result_status",
            }:
                raise ValueError("idempotency record schema is invalid")
            tenant_id = record.get("tenant_id")
            if not self._valid_id(tenant_id, optional=True):
                raise ValueError("idempotency tenant identifier is invalid")
            if not self._valid_id(record.get("project_id")) or not self._valid_id(record.get("request_id")):
                raise ValueError("idempotency project/request identifier is invalid")
            if self._key(tenant_id, record["project_id"], record["request_id"]) != key:
                raise ValueError("idempotency record key mismatch")
            fingerprint = record.get("fingerprint")
            if not isinstance(fingerprint, str) or _HASH_RE.fullmatch(fingerprint) is None:
                raise ValueError("idempotency fingerprint is invalid")
            if record.get("state") not in _STATES:
                raise ValueError("idempotency state value is invalid")
            token = record.get("lease_token")
            if not self._valid_id(token, optional=True):
                raise ValueError("idempotency lease token is invalid")
            if record["state"] in {"RESERVED", "STARTED"} and token is None:
                raise ValueError("active idempotency record has no lease token")
            if record["state"] in {"COMPLETED", "NOT_STARTED", "UNKNOWN"} and token is not None:
                raise ValueError("terminal idempotency record retains lease token")
            for field in ("lease_expires_at", "created_at", "updated_at"):
                value = record.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError("idempotency timestamp is invalid")
            attempts = record.get("attempts")
            if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 100000:
                raise ValueError("idempotency attempt count is invalid")
            result_status = record.get("result_status")
            if result_status is not None and (
                not isinstance(result_status, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", result_status) is None
            ):
                raise ValueError("idempotency result status is invalid")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            atomic_replace_with_retry(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def reserve_request(
        self, *, project_id: str, request_id: str, fingerprint: str,
        tenant_id: str | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        if not self._valid_id(project_id) or not self._valid_id(request_id) or not self._valid_id(tenant_id, optional=True):
            return {"reserved": False, "status": "INVALID_REQUEST_IDENTITY"}
        if not isinstance(fingerprint, str) or _HASH_RE.fullmatch(fingerprint) is None:
            return {"reserved": False, "status": "INVALID_REQUEST_FINGERPRINT"}
        try:
            current = self._now() if now is None else float(now)
            if not math.isfinite(current) or current < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return {"reserved": False, "status": "INVALID_IDEMPOTENCY_TIMESTAMP"}
        key = self._key(tenant_id, project_id, request_id)
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"reserved": False, "status": "IDEMPOTENCY_STATE_INVALID"}
            requests = state["requests"]
            record = requests.get(key)
            if record is not None:
                if (
                    record["tenant_id"] != tenant_id
                    or record["project_id"] != project_id
                    or record["request_id"] != request_id
                    or record["fingerprint"] != fingerprint
                ):
                    return {"reserved": False, "status": "REQUEST_ID_CONFLICT"}
                if record["state"] == "COMPLETED":
                    return {
                        "reserved": False, "status": "DUPLICATE_REQUEST",
                        "request_id": request_id, "result_status": record["result_status"],
                    }
                if record["state"] in {"STARTED", "UNKNOWN"}:
                    return {
                        "reserved": False, "status": "EXECUTION_OUTCOME_UNKNOWN",
                        "request_id": request_id, "result_status": record["result_status"],
                    }
                if record["state"] == "RESERVED" and record["lease_expires_at"] > current:
                    return {"reserved": False, "status": "REQUEST_IN_PROGRESS", "request_id": request_id}
                record["attempts"] += 1
                record["state"] = "RESERVED"
                record["lease_token"] = uuid.uuid4().hex
                record["lease_expires_at"] = current + self.lease_seconds
                record["updated_at"] = current
                record["result_status"] = None
            else:
                if len(requests) >= self.max_records:
                    return {"reserved": False, "status": "IDEMPOTENCY_CAPACITY_REACHED"}
                record = {
                    "tenant_id": tenant_id, "project_id": project_id,
                    "request_id": request_id, "fingerprint": fingerprint,
                    "state": "RESERVED", "lease_token": uuid.uuid4().hex,
                    "lease_expires_at": current + self.lease_seconds,
                    "attempts": 1, "created_at": current,
                    "updated_at": current, "result_status": None,
                }
                requests[key] = record
            try:
                self._save(state)
            except OSError:
                return {"reserved": False, "status": "IDEMPOTENCY_PERSISTENCE_FAILED"}
            return {
                "reserved": True, "status": "REQUEST_RESERVED",
                "request_id": request_id, "lease_token": record["lease_token"],
                "attempt": record["attempts"],
            }

    def mark_started(
        self, *, project_id: str, request_id: str, lease_token: str,
        tenant_id: str | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            project_id=project_id, request_id=request_id, lease_token=lease_token,
            tenant_id=tenant_id, target="STARTED", now=now,
        )

    def finalize(
        self, *, project_id: str, request_id: str, lease_token: str,
        outcome: str, result_status: str,
        tenant_id: str | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        target = _OUTCOMES.get(outcome)
        if target is None:
            return {"saved": False, "status": "INVALID_IDEMPOTENCY_OUTCOME"}
        if not isinstance(result_status, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", result_status) is None:
            return {"saved": False, "status": "INVALID_IDEMPOTENCY_RESULT_STATUS"}
        return self._transition(
            project_id=project_id, request_id=request_id, lease_token=lease_token,
            tenant_id=tenant_id, target=target, result_status=result_status, now=now,
        )

    def _transition(
        self, *, project_id: str, request_id: str, lease_token: str,
        tenant_id: str | None, target: str, result_status: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if (
            not self._valid_id(project_id) or not self._valid_id(request_id)
            or not self._valid_id(tenant_id, optional=True) or not self._valid_id(lease_token)
        ):
            return {"saved": False, "status": "INVALID_IDEMPOTENCY_IDENTITY"}
        try:
            current = self._now() if now is None else float(now)
            if not math.isfinite(current) or current < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return {"saved": False, "status": "INVALID_IDEMPOTENCY_TIMESTAMP"}
        key = self._key(tenant_id, project_id, request_id)
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"saved": False, "status": "IDEMPOTENCY_STATE_INVALID"}
            record = state["requests"].get(key)
            if not isinstance(record, dict) or record.get("lease_token") != lease_token:
                return {"saved": False, "status": "IDEMPOTENCY_LEASE_MISMATCH"}
            allowed = (
                target == "STARTED" and record["state"] == "RESERVED"
                or target == "NOT_STARTED" and record["state"] in {"RESERVED", "STARTED"}
                or target in {"COMPLETED", "UNKNOWN"} and record["state"] == "STARTED"
            )
            if not allowed:
                if record.get("state") == target and record.get("result_status") == result_status:
                    return {"saved": True, "status": "ALREADY_FINALIZED", "state": target}
                return {"saved": False, "status": "INVALID_IDEMPOTENCY_TRANSITION"}
            record["state"] = target
            record["lease_token"] = None
            record["lease_expires_at"] = current
            record["updated_at"] = current
            record["result_status"] = result_status
            try:
                self._save(state)
            except OSError:
                return {"saved": False, "status": "IDEMPOTENCY_PERSISTENCE_FAILED"}
            return {"saved": True, "status": "IDEMPOTENCY_STATE_UPDATED", "state": target}

    def get_status(
        self, *, project_id: str, request_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if not self._valid_id(project_id) or not self._valid_id(request_id) or not self._valid_id(tenant_id, optional=True):
            return {"status": "INVALID_REQUEST_IDENTITY"}
        key = self._key(tenant_id, project_id, request_id)
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                record = self._load()["requests"].get(key)
            except ValueError:
                return {"status": "IDEMPOTENCY_STATE_INVALID"}
            if not isinstance(record, dict):
                return {"status": "REQUEST_NOT_FOUND"}
            return {
                "status": "IDEMPOTENCY_STATUS", "state": record["state"],
                "request_id": record["request_id"], "project_id": record["project_id"],
                "tenant_id": record["tenant_id"], "attempts": record["attempts"],
                "result_status": record["result_status"],
                "created_at": record["created_at"], "updated_at": record["updated_at"],
            }
