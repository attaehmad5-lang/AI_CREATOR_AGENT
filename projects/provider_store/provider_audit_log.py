"""Append-only, hash-chained audit events for provider operations.

Only identifiers, operation/outcome codes, and timestamps are persisted. Payloads,
prompts, tokens, request bodies, and provider response bodies are never accepted.
"""
from __future__ import annotations

import hashlib
import hmac
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

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z")
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]{6,}|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b)"
)
_ZERO_HASH = "0" * 64


class ProviderAuditLog:
    """Store verifiable append-only events with cross-process serialization."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_entries: int = 1_000_000,
        clock: Any = None,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or not 1 <= max_entries <= 5_000_000:
            raise ValueError("max_entries must be within 1..5000000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_entries = max_entries
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "entries": []}
        self._reload()

    @staticmethod
    def _safe_audit_value(value: str) -> bool:
        return _SECRET_RE.search(value) is None

    @staticmethod
    def _hash_entry(entry: dict[str, Any]) -> str:
        payload = {key: value for key, value in entry.items() if key != "entry_hash"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError, OverflowError):
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "entries": []}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider audit log JSON") from exc
        entries = loaded.get("entries") if isinstance(loaded, dict) else None
        if not isinstance(entries, list) or len(entries) > self.max_entries:
            raise ValueError("invalid provider audit log schema or size")
        previous = _ZERO_HASH
        for index, entry in enumerate(entries, start=1):
            if (
                not isinstance(entry, dict)
                or entry.get("sequence") != index
                or not self._valid_timestamp(entry.get("timestamp"))
                or not isinstance(entry.get("actor_id"), str) or not _ID_RE.fullmatch(entry["actor_id"])
                or not isinstance(entry.get("operation"), str) or not _CODE_RE.fullmatch(entry["operation"])
                or not isinstance(entry.get("resource_type"), str) or not _CODE_RE.fullmatch(entry["resource_type"])
                or not isinstance(entry.get("resource_id"), str) or not _ID_RE.fullmatch(entry["resource_id"])
                or not isinstance(entry.get("outcome"), str) or not _CODE_RE.fullmatch(entry["outcome"])
                or (entry.get("request_id") is not None and (
                    not isinstance(entry.get("request_id"), str) or not _ID_RE.fullmatch(entry["request_id"])
                ))
                or entry.get("previous_hash") != previous
                or not isinstance(entry.get("entry_hash"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["entry_hash"])
                or not hmac.compare_digest(entry["entry_hash"], self._hash_entry(entry))
            ):
                raise ValueError("provider audit log integrity validation failed")
            previous = entry["entry_hash"]
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

    def append(
        self,
        *,
        actor_id: str,
        operation: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(actor_id, str) or not _ID_RE.fullmatch(actor_id) or not self._safe_audit_value(actor_id):
            return {"status": "INVALID_AUDIT_ACTOR", "saved": False}
        if not isinstance(operation, str) or not _CODE_RE.fullmatch(operation) or not self._safe_audit_value(operation):
            return {"status": "INVALID_AUDIT_OPERATION", "saved": False}
        if not isinstance(resource_type, str) or not _CODE_RE.fullmatch(resource_type) or not self._safe_audit_value(resource_type):
            return {"status": "INVALID_AUDIT_RESOURCE_TYPE", "saved": False}
        if not isinstance(resource_id, str) or not _ID_RE.fullmatch(resource_id) or not self._safe_audit_value(resource_id):
            return {"status": "INVALID_AUDIT_RESOURCE_ID", "saved": False}
        if not isinstance(outcome, str) or not _CODE_RE.fullmatch(outcome) or not self._safe_audit_value(outcome):
            return {"status": "INVALID_AUDIT_OUTCOME", "saved": False}
        if request_id is not None and (
            not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id) or not self._safe_audit_value(request_id)
        ):
            return {"status": "INVALID_AUDIT_REQUEST_ID", "saved": False}
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return {"status": "INVALID_AUDIT_CLOCK", "saved": False}
        with self._transaction():
            entries = self.data["entries"]
            if len(entries) >= self.max_entries:
                return {"status": "AUDIT_LOG_FULL", "saved": False}
            entry = {
                "sequence": len(entries) + 1,
                "timestamp": now.astimezone(timezone.utc).isoformat(),
                "actor_id": actor_id,
                "operation": operation,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "request_id": request_id,
                "outcome": outcome,
                "previous_hash": entries[-1]["entry_hash"] if entries else _ZERO_HASH,
            }
            entry["entry_hash"] = self._hash_entry(entry)
            entries.append(entry)
            self._save()
            return {"status": "AUDIT_EVENT_SAVED", "saved": True, "sequence": entry["sequence"], "entry_hash": entry["entry_hash"]}

    def verify_chain(self) -> dict[str, Any]:
        try:
            with self._transaction():
                return {
                    "valid": True,
                    "status": "AUDIT_CHAIN_VALID",
                    "entry_count": len(self.data["entries"]),
                    "head_hash": self.data["entries"][-1]["entry_hash"] if self.data["entries"] else _ZERO_HASH,
                }
        except (OSError, ValueError):
            return {"valid": False, "status": "AUDIT_CHAIN_INVALID"}
