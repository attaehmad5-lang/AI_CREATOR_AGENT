"""Persistent explicit approval workflow for paid provider usage."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


class PaidUsageApprovalManager:
    """Create, approve, validate, revoke and audit project/provider/request-scoped approvals."""

    VERSION = 1
    _ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
    _UNIT = re.compile(r"[A-Za-z0-9._/-]{1,32}\Z")
    _SECRET_MARKERS = ("token=", "api_key=", "password=", "secret=", "bearer ")

    def __init__(self, path: str | Path, *, clock: Any = None, authorizer: Any = None) -> None:
        if not isinstance(path, (str, Path)) or not str(path).strip() or "\x00" in str(path):
            raise ValueError("invalid approval store path")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if authorizer is not None and not callable(authorizer):
            raise ValueError("authorizer must be callable")
        self.authorizer = authorizer
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, interprocess_file_lock(self.lock_path):
            if not self.path.exists():
                self._write({"version": self.VERSION, "approvals": {}})

    @classmethod
    def _safe_id(cls, value: Any) -> bool:
        return (
            isinstance(value, str) and bool(cls._ID.fullmatch(value))
            and not any(marker in value.casefold() for marker in cls._SECRET_MARKERS)
        )

    @classmethod
    def _safe_unit(cls, value: Any) -> bool:
        return isinstance(value, str) and bool(cls._UNIT.fullmatch(value))

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("approval clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("approval store is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != self.VERSION or not isinstance(data.get("approvals"), dict):
            raise ValueError("approval store schema is invalid")
        for approval_id, record in data["approvals"].items():
            if (
                not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id)
                or not isinstance(record, dict)
                or record.get("approval_id") != approval_id
                or record.get("status") not in {"PENDING", "APPROVED", "REVOKED", "EXPIRED"}
                or not all(self._safe_id(record.get(key)) for key in ("project_id", "provider_id", "request_id", "requested_by"))
                or not self._safe_unit(record.get("unit"))
                or finite_number(record.get("requested_cost"), minimum=0) is None
                or not isinstance(record.get("reason"), str)
                or not isinstance(record.get("requested_at"), str)
            ):
                raise ValueError("approval store contains an invalid record")
            if record.get("status") == "APPROVED":
                if (
                    not self._safe_id(record.get("approved_by"))
                    or finite_number(record.get("max_amount"), minimum=0) is None
                    or not isinstance(record.get("approved_at"), str)
                    or not isinstance(record.get("expires_at"), str)
                ):
                    raise ValueError("approved record is invalid")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp, self.path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        with self._lock, interprocess_file_lock(self.lock_path):
            data = self._read()
            yield data
            self._write(data)

    def request_approval(
        self, *, project_id: str, provider_id: str, request_id: str,
        estimated_cost: float, unit: str = "USD", requested_by: str,
        reason: str,
    ) -> dict[str, Any]:
        if not all(self._safe_id(value) for value in (project_id, provider_id, request_id, requested_by)):
            return {"status": "INVALID_APPROVAL_SCOPE", "approval_created": False}
        amount = finite_number(estimated_cost, minimum=0)
        if amount is None or amount <= 0 or not self._safe_unit(unit):
            return {"status": "INVALID_APPROVAL_AMOUNT_OR_UNIT", "approval_created": False}
        if (
            not isinstance(reason, str) or not reason.strip() or len(reason) > 500
            or any(marker in reason.casefold() for marker in self._SECRET_MARKERS)
            or any(ord(char) < 32 and char not in "\t\n\r" for char in reason)
        ):
            return {"status": "INVALID_APPROVAL_REASON", "approval_created": False}
        now = self._now().isoformat()
        with self._transaction() as data:
            for existing in data["approvals"].values():
                if (
                    existing.get("project_id") == project_id
                    and existing.get("provider_id") == provider_id
                    and existing.get("request_id") == request_id
                    and existing.get("status") in {"PENDING", "APPROVED"}
                ):
                    return {"status": "APPROVAL_ALREADY_EXISTS", "approval_created": False, "approval_id": existing["approval_id"]}
            approval_id = uuid.uuid4().hex
            data["approvals"][approval_id] = {
                "approval_id": approval_id, "project_id": project_id,
                "provider_id": provider_id, "request_id": request_id,
                "requested_cost": amount, "unit": unit, "requested_by": requested_by,
                "reason": reason.strip(), "status": "PENDING", "requested_at": now,
            }
        return {"status": "APPROVAL_PENDING", "approval_created": True, "approval_id": approval_id}

    def approve(
        self, approval_id: str, *, approved_by: str,
        max_amount: float | None = None, expires_in_seconds: int = 900,
    ) -> dict[str, Any]:
        if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id) or not self._safe_id(approved_by):
            return {"status": "INVALID_APPROVAL_IDENTITY", "approved": False}
        if not isinstance(expires_in_seconds, int) or isinstance(expires_in_seconds, bool) or not 1 <= expires_in_seconds <= 86400:
            return {"status": "INVALID_APPROVAL_EXPIRY", "approved": False}
        with self._transaction() as data:
            record = data["approvals"].get(approval_id)
            if record is None:
                return {"status": "APPROVAL_NOT_FOUND", "approved": False}
            if record.get("status") != "PENDING":
                return {"status": "APPROVAL_NOT_PENDING", "approved": False}
            if self.authorizer is None:
                return {"status": "APPROVER_AUTHORIZER_REQUIRED", "approved": False}
            try:
                authorized = self.authorizer("approve", approved_by, copy.deepcopy(record))
            except Exception:
                return {"status": "APPROVER_AUTHORIZATION_FAILED", "approved": False}
            if authorized is not True:
                return {"status": "APPROVER_NOT_AUTHORIZED", "approved": False}
            ceiling = record["requested_cost"] if max_amount is None else finite_number(max_amount, minimum=0)
            if ceiling is None or ceiling < record["requested_cost"]:
                return {"status": "APPROVAL_LIMIT_BELOW_REQUEST", "approved": False}
            now = self._now()
            record.update({
                "status": "APPROVED", "approved_by": approved_by,
                "approved_at": now.isoformat(), "max_amount": ceiling,
                "expires_at": (now + timedelta(seconds=expires_in_seconds)).isoformat(),
            })
        return {"status": "APPROVED", "approved": True, "approval_id": approval_id}

    def authorize(
        self, approval_id: str | None, *, project_id: str, provider_id: str,
        request_id: str, estimated_cost: float, unit: str = "USD",
    ) -> dict[str, Any]:
        if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id):
            return {"status": "PAID_APPROVAL_REQUIRED", "approved": False}
        amount = finite_number(estimated_cost, minimum=0)
        if amount is None or amount <= 0 or not self._safe_unit(unit):
            return {"status": "INVALID_APPROVAL_AMOUNT_OR_UNIT", "approved": False}
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                data = self._read()
            except ValueError:
                return {"status": "APPROVAL_STORE_INVALID", "approved": False}
        record = data["approvals"].get(approval_id)
        if not isinstance(record, dict):
            return {"status": "APPROVAL_NOT_FOUND", "approved": False}
        if record.get("status") != "APPROVED":
            return {"status": "PAID_APPROVAL_REQUIRED", "approved": False}
        if any(record.get(key) != value for key, value in (
            ("project_id", project_id), ("provider_id", provider_id),
            ("request_id", request_id), ("unit", unit),
        )):
            return {"status": "APPROVAL_SCOPE_MISMATCH", "approved": False}
        if amount > record.get("max_amount", -1):
            return {"status": "APPROVAL_AMOUNT_EXCEEDED", "approved": False}
        try:
            if self._now() >= datetime.fromisoformat(record["expires_at"]):
                return {"status": "APPROVAL_EXPIRED", "approved": False}
        except (ValueError, TypeError):
            return {"status": "APPROVAL_STORE_INVALID", "approved": False}
        return {
            "status": "PAID_APPROVAL_VALID", "approved": True,
            "approval_id": approval_id, "approved_by": record["approved_by"],
            "max_amount": record["max_amount"], "unit": record["unit"],
        }

    def revoke(self, approval_id: str, *, revoked_by: str) -> dict[str, Any]:
        if not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{32}", approval_id) or not self._safe_id(revoked_by):
            return {"status": "INVALID_APPROVAL_IDENTITY", "revoked": False}
        with self._transaction() as data:
            record = data["approvals"].get(approval_id)
            if not isinstance(record, dict):
                return {"status": "APPROVAL_NOT_FOUND", "revoked": False}
            if record.get("status") not in {"PENDING", "APPROVED"}:
                return {"status": "APPROVAL_NOT_REVOCABLE", "revoked": False}
            if self.authorizer is None:
                return {"status": "APPROVER_AUTHORIZER_REQUIRED", "revoked": False}
            try:
                authorized = self.authorizer("revoke", revoked_by, copy.deepcopy(record))
            except Exception:
                return {"status": "APPROVER_AUTHORIZATION_FAILED", "revoked": False}
            if authorized is not True:
                return {"status": "APPROVER_NOT_AUTHORIZED", "revoked": False}
            record.update({"status": "REVOKED", "revoked_by": revoked_by, "revoked_at": self._now().isoformat()})
        return {"status": "REVOKED", "revoked": True, "approval_id": approval_id}

    def list_approvals(self) -> dict[str, Any]:
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                data = self._read()
            except ValueError:
                return {"status": "APPROVAL_STORE_INVALID", "approvals": []}
        return {"status": "OK", "approvals": [copy.deepcopy(v) for _, v in sorted(data["approvals"].items())]}
