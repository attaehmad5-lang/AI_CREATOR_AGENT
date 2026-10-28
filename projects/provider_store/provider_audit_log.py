"""Append-only, hash-chained provider audit events with an allowlisted schema."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import interprocess_file_lock

_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_ZERO_HASH = "0" * 64


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class ProviderAuditLog:
    """Durable audit records that never accept arbitrary payloads or secret fields."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = 10_000_000,
        clock: Any = None,
    ) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1024 <= max_bytes <= 100_000_000:
            raise ValueError("max_bytes must be an integer between 1024 and 100000000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_bytes = max_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _read_records(self) -> tuple[list[dict[str, Any]] | None, str | None]:
        if not self.path.exists():
            return [], None
        if self.path.is_symlink():
            return None, "AUDIT_LOG_SYMLINK_REJECTED"
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None, "AUDIT_LOG_READ_FAILED"
        if len(raw) > self.max_bytes:
            return None, "AUDIT_LOG_SIZE_LIMIT"
        if raw and not raw.endswith(b"\n"):
            return None, "AUDIT_LOG_TRUNCATED"
        records: list[dict[str, Any]] = []
        previous_hash = _ZERO_HASH
        for line_number, line in enumerate(raw.splitlines(), start=1):
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, "AUDIT_LOG_INVALID_JSON"
            if not isinstance(record, dict):
                return None, "AUDIT_LOG_INVALID_RECORD"
            record_hash = record.get("record_hash")
            body = {key: value for key, value in record.items() if key != "record_hash"}
            if (
                record.get("previous_hash") != previous_hash
                or not isinstance(record_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", record_hash)
                or hashlib.sha256(_canonical_json(body)).hexdigest() != record_hash
            ):
                return None, "AUDIT_LOG_CHAIN_INVALID"
            if not self._valid_record_fields(body):
                return None, "AUDIT_LOG_INVALID_RECORD"
            records.append(record)
            previous_hash = record_hash
        return records, None

    @staticmethod
    def _valid_record_fields(record: dict[str, Any]) -> bool:
        if set(record) != {
            "timestamp", "event", "status", "outcome", "provider_id",
            "actor_id", "request_id", "reason_code", "previous_hash",
        }:
            return False
        if not isinstance(record.get("timestamp"), str):
            return False
        try:
            timestamp = datetime.fromisoformat(record["timestamp"])
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            return False
        if not isinstance(record.get("event"), str) or not _EVENT.fullmatch(record["event"]):
            return False
        if not isinstance(record.get("status"), str) or not _CODE.fullmatch(record["status"]):
            return False
        if record.get("outcome") not in {"OK", "ERROR", "CANCELLED", "DENIED", "UNKNOWN"}:
            return False
        for key in ("provider_id", "actor_id", "request_id"):
            value = record.get(key)
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                return False
        reason = record.get("reason_code")
        if reason is not None and (not isinstance(reason, str) or not _CODE.fullmatch(reason)):
            return False
        previous = record.get("previous_hash")
        return isinstance(previous, str) and bool(re.fullmatch(r"[0-9a-f]{64}", previous))

    def record(
        self,
        event: str,
        *,
        status: str,
        outcome: str,
        provider_id: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            return {"status": "INVALID_AUDIT_EVENT", "saved": False}
        if not isinstance(status, str) or not _CODE.fullmatch(status):
            return {"status": "INVALID_AUDIT_STATUS", "saved": False}
        if not isinstance(outcome, str) or outcome not in {"OK", "ERROR", "CANCELLED", "DENIED", "UNKNOWN"}:
            return {"status": "INVALID_AUDIT_OUTCOME", "saved": False}
        for key, value in (("provider_id", provider_id), ("actor_id", actor_id), ("request_id", request_id)):
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                return {"status": "INVALID_AUDIT_" + key.upper(), "saved": False}
        if reason_code is not None and (
            not isinstance(reason_code, str) or not _CODE.fullmatch(reason_code)
        ):
            return {"status": "INVALID_AUDIT_REASON", "saved": False}
        timestamp = self.clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            return {"status": "INVALID_AUDIT_CLOCK", "saved": False}
        base = {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "event": event,
            "status": status,
            "outcome": outcome,
            "provider_id": provider_id,
            "actor_id": actor_id,
            "request_id": request_id,
            "reason_code": reason_code,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_file_lock(self.lock_path):
            records, error = self._read_records()
            if error is not None or records is None:
                return {"status": error or "AUDIT_LOG_READ_FAILED", "saved": False}
            previous_hash = records[-1]["record_hash"] if records else _ZERO_HASH
            body = {**base, "previous_hash": previous_hash}
            record_hash = hashlib.sha256(_canonical_json(body)).hexdigest()
            record = {**body, "record_hash": record_hash}
            encoded = _canonical_json(record) + b"\n"
            current_size = self.path.stat().st_size if self.path.exists() else 0
            if current_size + len(encoded) > self.max_bytes:
                return {"status": "AUDIT_LOG_SIZE_LIMIT", "saved": False}
            if self.path.is_symlink():
                return {"status": "AUDIT_LOG_SYMLINK_REJECTED", "saved": False}
            try:
                with self.path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                return {"status": "AUDIT_LOG_WRITE_FAILED", "saved": False}
        return {"status": "AUDIT_EVENT_RECORDED", "saved": True, "record_hash": record_hash}

    def verify_chain(self) -> dict[str, Any]:
        with interprocess_file_lock(self.lock_path):
            records, error = self._read_records()
        if error is not None or records is None:
            return {"status": error or "AUDIT_LOG_READ_FAILED", "valid": False, "count": 0}
        return {
            "status": "AUDIT_CHAIN_VALID" if records else "AUDIT_LOG_EMPTY",
            "valid": True,
            "count": len(records),
            "last_hash": records[-1]["record_hash"] if records else _ZERO_HASH,
        }
