"""Persistent provider/region outage state used to fail over around partial outages."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

OUTAGE_STATE_PATH = Path(__file__).with_name("provider_outages.json")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
_REASON_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")


class ProviderOutageManager:
    """Persist operator-declared outages; never infer availability from catalog labels."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, clock: Any = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path) if path is not None else OUTAGE_STATE_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
        self._validate_document(self.data)

    def _load_current(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "providers": {}, "regions": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("provider outage state is unreadable") from exc
        if not isinstance(loaded, dict):
            raise ValueError("invalid provider outage state")
        return loaded

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if (
            not isinstance(data, dict) or data.get("version") != "1.0"
            or not isinstance(data.get("providers"), dict)
            or not isinstance(data.get("regions"), dict)
        ):
            raise ValueError("invalid provider outage state schema")
        for collection in ("providers", "regions"):
            for identity, record in data[collection].items():
                if (
                    not isinstance(identity, str) or not _ID_RE.fullmatch(identity)
                    or not isinstance(record, dict)
                    or not isinstance(record.get("reason_code"), str)
                    or not _REASON_RE.fullmatch(record["reason_code"])
                    or not isinstance(record.get("started_at"), str)
                    or not isinstance(record.get("updated_at"), str)
                ):
                    raise ValueError("invalid provider outage record")

    def _save_unlocked(self) -> None:
        self._validate_document(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _mark(self, collection: str, identity: str, reason_code: str) -> dict[str, Any]:
        if not isinstance(identity, str) or not _ID_RE.fullmatch(identity):
            return {"status": "INVALID_OUTAGE_IDENTITY", "saved": False}
        if not isinstance(reason_code, str) or not _REASON_RE.fullmatch(reason_code):
            return {"status": "INVALID_OUTAGE_REASON", "saved": False}
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            previous = self.data[collection].get(identity)
            started_at = previous.get("started_at", now) if isinstance(previous, dict) else now
            self.data[collection][identity] = {
                "reason_code": reason_code, "started_at": started_at, "updated_at": now,
            }
            self._save_unlocked()
        return {"status": "OUTAGE_RECORDED", "saved": True, "identity": identity}

    def _clear(self, collection: str, identity: str) -> dict[str, Any]:
        if not isinstance(identity, str) or not _ID_RE.fullmatch(identity):
            return {"status": "INVALID_OUTAGE_IDENTITY", "saved": False}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            if identity not in self.data[collection]:
                return {"status": "OUTAGE_NOT_FOUND", "saved": False}
            del self.data[collection][identity]
            self._save_unlocked()
        return {"status": "OUTAGE_CLEARED", "saved": True, "identity": identity}

    def mark_provider_outage(self, provider_id: str, reason_code: str = "OPERATOR_DECLARED") -> dict[str, Any]:
        return self._mark("providers", provider_id, reason_code)

    def clear_provider_outage(self, provider_id: str) -> dict[str, Any]:
        return self._clear("providers", provider_id)

    def mark_region_outage(self, region_id: str, reason_code: str = "OPERATOR_DECLARED") -> dict[str, Any]:
        return self._mark("regions", region_id, reason_code)

    def clear_region_outage(self, region_id: str) -> dict[str, Any]:
        return self._clear("regions", region_id)

    def evaluate_provider(self, provider: dict[str, Any], *, required_region: str | None = None) -> dict[str, Any]:
        if not isinstance(provider, dict) or not isinstance(provider.get("id"), str) or not _ID_RE.fullmatch(provider["id"]):
            return {"status": "INVALID_PROVIDER_RECORD", "available": False}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            provider_outage = self.data["providers"].get(provider["id"])
            if provider_outage is not None:
                return {"status": "PROVIDER_OUTAGE", "available": False, "reason_code": provider_outage["reason_code"]}
            if required_region is not None:
                region_outage = self.data["regions"].get(required_region)
                if region_outage is not None:
                    return {"status": "REQUIRED_REGION_OUTAGE", "available": False, "reason_code": region_outage["reason_code"]}
            actual_region = provider.get("region_id", provider.get("region"))
            if isinstance(actual_region, str) and actual_region in self.data["regions"]:
                return {"status": "PROVIDER_REGION_OUTAGE", "available": False, "reason_code": self.data["regions"][actual_region]["reason_code"]}
        return {"status": "AVAILABLE", "available": True}

    def snapshot(self) -> dict[str, Any]:
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            return json.loads(json.dumps(self.data))
