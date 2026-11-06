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
            if record["state"] in {"COMPLETED", "NOT_STARTED", "UNKNOWN"} and result_status is None:
                raise ValueError("terminal idempotency record has no result status")
            if record["updated_at"] < record["created_at"] or record["lease_expires_at"] < record["created_at"]:
                raise ValueError("idempotency timestamps are inconsistent")
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
            if now is None:
                current = self._now()
            elif isinstance(now, bool) or not isinstance(now, (int, float)):
                raise ValueError
            else:
                current = float(now)
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
                if current < record["updated_at"]:
                    return {"reserved": False, "status": "INVALID_IDEMPOTENCY_TIMESTAMP"}
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
            if now is None:
                current = self._now()
            elif isinstance(now, bool) or not isinstance(now, (int, float)):
                raise ValueError
            else:
                current = float(now)
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
            if not isinstance(record, dict):
                return {"saved": False, "status": "IDEMPOTENCY_LEASE_MISMATCH"}
            if (
                record.get("state") == target
                and record.get("result_status") == result_status
                and record.get("lease_token") is None
            ):
                return {"saved": True, "status": "ALREADY_FINALIZED", "state": target}
            if record.get("lease_token") != lease_token:
                return {"saved": False, "status": "IDEMPOTENCY_LEASE_MISMATCH"}
            if current < record["updated_at"]:
                return {"saved": False, "status": "INVALID_IDEMPOTENCY_TIMESTAMP"}
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
            if target == "STARTED":
                # Keep the worker lease while the external call may be in flight.
                # Clearing it here would make STARTED records unverifiable and
                # would allow unsafe duplicate execution after a reload.
                record["lease_expires_at"] = max(
                    record["lease_expires_at"], current,
                )
                record["result_status"] = None
            else:
                record["lease_token"] = None
                record["lease_expires_at"] = current
                record["result_status"] = result_status
            record["updated_at"] = current
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


def _fingerprint_value(value: Any) -> Any:
    """Normalize request data for hashing without persisting the source values."""
    from collections.abc import Mapping

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite fingerprint value")
        return value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("unsupported fingerprint mapping key")
            result[key] = _fingerprint_value(item)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [_fingerprint_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_fingerprint_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    raise ValueError("unsupported fingerprint value")


def provider_execution_idempotent(function: Any) -> Any:
    """Claim an execution key before service work and finalize it conservatively."""
    from functools import wraps

    @wraps(function)
    def wrapped(service: Any, *args: Any, **kwargs: Any) -> Any:
        manager = getattr(service, "request_idempotency_manager", None)
        if manager is None:
            return function(service, *args, **kwargs)
        request_id = kwargs.get("request_id")
        project_id = kwargs.get("project_id")
        tenant_id = kwargs.get("tenant_id")
        adapter = kwargs.get("adapter")
        if not callable(getattr(manager, "reserve_request", None)) or not callable(
            getattr(manager, "get_status", None)
        ) or not callable(getattr(manager, "mark_started", None)) or not callable(
            getattr(manager, "finalize", None)
        ):
            return {"status": "IDEMPOTENCY_MANAGER_UNAVAILABLE", "executed": False}
        try:
            adapter_name = (
                f"{getattr(adapter, '__module__', '')}.{getattr(adapter, '__qualname__', type(adapter).__name__)}"
            )
            fingerprint_payload = {
                "category": kwargs.get("category"),
                "project_id": project_id,
                "tenant_id": tenant_id,
                "principal_id": kwargs.get("principal_id"),
                "modality": kwargs.get("modality"),
                "estimated_cost": kwargs.get("estimated_cost"),
                "estimated_cost_unit": kwargs.get("estimated_cost_unit", "USD"),
                "data_categories": kwargs.get("data_categories", ("user_content",)),
                "retention_days": kwargs.get("retention_days", 0),
                "provider_training_use": kwargs.get("provider_training_use", False),
                "user_training_consent": kwargs.get("user_training_consent", False),
                "output_path": kwargs.get("output_path"),
                "require_modality_binding": kwargs.get("require_modality_binding", False),
                "use_managed_credential": kwargs.get("use_managed_credential", False),
                "require_fresh_health_check": kwargs.get("require_fresh_health_check", False),
                "health_stale_after_seconds": kwargs.get("health_stale_after_seconds", 300.0),
                "health_max_latency_ms": kwargs.get("health_max_latency_ms"),
                "adapter": adapter_name,
                "adapter_options": kwargs.get("adapter_options") or {},
                "route_options": {
                    key: value for key, value in kwargs.items()
                    if key not in {
                        "request_id", "trace_id", "traceparent", "adapter",
                        "adapter_options", "principal_id", "tenant_id",
                        "project_id", "category", "modality", "estimated_cost",
                        "estimated_cost_unit",
                    }
                },
            }
            canonical = json.dumps(
                _fingerprint_value(fingerprint_payload),
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            fingerprint = hashlib.sha256(canonical).hexdigest()
        except (TypeError, ValueError, OverflowError):
            return {"status": "IDEMPOTENCY_FINGERPRINT_UNSUPPORTED", "executed": False}

        try:
            reservation = manager.reserve_request(
                project_id=project_id, request_id=request_id,
                tenant_id=tenant_id, fingerprint=fingerprint,
            )
        except Exception:
            return {"status": "IDEMPOTENCY_RESERVATION_FAILED", "executed": False}
        if not isinstance(reservation, dict) or reservation.get("reserved") is not True:
            status = reservation.get("status") if isinstance(reservation, dict) else None
            return {
                "status": status if isinstance(status, str) else "IDEMPOTENCY_RESERVATION_REJECTED",
                "executed": False, "request_id": request_id,
            }
        lease_token = reservation.get("lease_token")
        if not isinstance(lease_token, str):
            return {"status": "IDEMPOTENCY_LEASE_INVALID", "executed": False, "request_id": request_id}
        kwargs["_request_idempotency_lease_token"] = lease_token

        try:
            result = function(service, *args, **kwargs)
        except Exception:
            try:
                current = manager.get_status(
                    project_id=project_id, request_id=request_id, tenant_id=tenant_id,
                )
                state = current.get("state") if isinstance(current, dict) else None
                if state == "STARTED":
                    manager.finalize(
                        project_id=project_id, request_id=request_id, tenant_id=tenant_id,
                        lease_token=lease_token, outcome="unknown",
                        result_status="EXECUTION_OUTCOME_UNKNOWN",
                    )
                elif state == "RESERVED":
                    manager.finalize(
                        project_id=project_id, request_id=request_id, tenant_id=tenant_id,
                        lease_token=lease_token, outcome="not_started",
                        result_status="EXECUTION_EXCEPTION_BEFORE_START",
                    )
            except Exception:
                pass
            raise

        status = result.get("status") if isinstance(result, dict) else "INVALID_EXECUTION_RESPONSE"
        try:
            current = manager.get_status(
                project_id=project_id, request_id=request_id, tenant_id=tenant_id,
            )
            state = current.get("state") if isinstance(current, dict) else None
            if state == "RESERVED":
                outcome = "not_started"
                final_status = status if isinstance(status, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", status) else "EXECUTION_NOT_STARTED"
            elif state == "STARTED":
                if status in {"EXECUTION_OUTCOME_UNKNOWN", "INCONSISTENT_START_RESULT"} or (
                    isinstance(result, dict) and result.get("reservations_retained") is True
                ):
                    outcome = "unknown"
                    final_status = status if isinstance(status, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", status) else "EXECUTION_OUTCOME_UNKNOWN"
                elif status in {"EXECUTION_NOT_STARTED", "RESERVATION_RELEASE_FAILED"}:
                    outcome = "not_started"
                    final_status = status
                else:
                    outcome = "completed"
                    final_status = status if isinstance(status, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,79}", status) else "EXECUTION_COMPLETED"
            else:
                return result
            finalized = manager.finalize(
                project_id=project_id, request_id=request_id, tenant_id=tenant_id,
                lease_token=lease_token, outcome=outcome, result_status=final_status,
            )
            if not isinstance(finalized, dict) or finalized.get("saved") is not True:
                return {
                    "status": "IDEMPOTENCY_FINALIZATION_FAILED",
                    "executed": bool(state == "STARTED"),
                    "side_effects_may_exist": state == "STARTED",
                    "request_id": request_id,
                    "operation_result": result if isinstance(result, dict) else {"status": "INVALID_EXECUTION_RESPONSE"},
                }
        except Exception:
            return {
                "status": "IDEMPOTENCY_FINALIZATION_FAILED",
                "executed": True, "side_effects_may_exist": True,
                "request_id": request_id,
                "operation_result": result if isinstance(result, dict) else {"status": "INVALID_EXECUTION_RESPONSE"},
            }
        return result
    return wrapped
