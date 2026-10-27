"""Tamper-evident audit log (hash-chained JSONL, secrets redacted)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .errors import StoreCorruptError
from .redact import redact
from .storage import AtomicJsonStore


class AuditLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._guard = AtomicJsonStore(self.path.with_name(self.path.name + ".guard"))

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else "0" * 64

    def append(self, action: str, **details: Any) -> dict:
        with self._guard.lock():
            entry = {"ts": time.time(), "action": action, "details": redact(details), "prev": self._last_hash()}
            body = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            entry["hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            return entry

    def entries(self) -> list:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify(self) -> int:
        """Verify the hash chain; returns number of entries or raises StoreCorruptError."""
        prev = "0" * 64
        count = 0
        for entry in self.entries():
            claimed = entry.pop("hash", None)
            if entry.get("prev") != prev:
                raise StoreCorruptError(f"audit chain broken at entry {count}")
            body = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != claimed:
                raise StoreCorruptError(f"audit entry tampered at {count}")
            prev = claimed
            count += 1
        return count
