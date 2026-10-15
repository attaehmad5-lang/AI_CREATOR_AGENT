"""Generic HMAC-SHA256 webhook authentication and replay protection.

Provider-specific adapters must use the exact signing convention documented by their
provider. This helper uses: HMAC-SHA256(secret, ASCII(timestamp) + b"." + raw_body).
Secrets are resolved at verification time and are never persisted or returned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderWebhookVerifier:
    def __init__(
        self,
        ledger_path: str | os.PathLike[str],
        *,
        secret_resolver: Callable[[str], str | bytes | None],
        max_age_seconds: int = 300,
        max_future_skew_seconds: int = 30,
        max_body_bytes: int = 1_000_000,
        max_replay_entries: int = 10_000,
    ) -> None:
        if not isinstance(ledger_path, (str, os.PathLike)) or not str(ledger_path).strip():
            raise ValueError("ledger_path is required")
        if not callable(secret_resolver):
            raise ValueError("secret_resolver must be callable")
        for name, value, minimum, maximum in (
            ("max_age_seconds", max_age_seconds, 1, 86400),
            ("max_future_skew_seconds", max_future_skew_seconds, 0, 3600),
            ("max_body_bytes", max_body_bytes, 1, 50_000_000),
            ("max_replay_entries", max_replay_entries, 1, 1_000_000),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the allowed range")
        self.path = Path(ledger_path)
        self.secret_resolver = secret_resolver
        self.max_age_seconds = max_age_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.max_body_bytes = max_body_bytes
        self.max_replay_entries = max_replay_entries
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "seen": {}}
        self._reload()

    @staticmethod
    def signature_for(secret: str | bytes, timestamp: str | int, raw_body: bytes) -> str:
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            secret_bytes = secret
        else:
            raise ValueError("secret must be text or bytes")
        if not secret_bytes or not isinstance(raw_body, bytes):
            raise ValueError("secret and raw body are required")
        timestamp_text = str(timestamp)
        if not timestamp_text.isascii() or not timestamp_text.isdigit():
            raise ValueError("timestamp must be a decimal epoch value")
        message = timestamp_text.encode("ascii") + b"." + raw_body
        return hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()

    def _reload(self) -> None:
        if not self.path.exists():
            self._data = {"version": 1, "seen": {}}
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid webhook replay ledger") from exc
        if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("seen"), dict):
            raise ValueError("Invalid webhook replay ledger schema")
        for key, record in loaded["seen"].items():
            if (
                not isinstance(key, str) or len(key) != 64
                or any(ch not in "0123456789abcdef" for ch in key)
                or not isinstance(record, dict)
                or not isinstance(record.get("provider_id"), str)
                or not record["provider_id"].strip()
                or not isinstance(record.get("timestamp"), int)
                or isinstance(record.get("timestamp"), bool)
            ):
                raise ValueError("Invalid webhook replay record")
        if len(loaded["seen"]) > self.max_replay_entries:
            raise ValueError("Webhook replay ledger exceeds configured capacity")
        self._data = loaded

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._data, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def verify(
        self,
        provider_id: str,
        raw_body: bytes,
        signature: str,
        timestamp: str | int,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 200:
            return {"status": "INVALID_PROVIDER", "verified": False}
        if not isinstance(raw_body, bytes):
            return {"status": "INVALID_BODY", "verified": False}
        if len(raw_body) > self.max_body_bytes:
            return {"status": "BODY_TOO_LARGE", "verified": False}
        if not isinstance(signature, str):
            return {"status": "INVALID_SIGNATURE_FORMAT", "verified": False}
        supplied = signature[7:] if signature.startswith("sha256=") else signature
        if len(supplied) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in supplied):
            return {"status": "INVALID_SIGNATURE_FORMAT", "verified": False}
        if isinstance(timestamp, bool) or not isinstance(timestamp, (str, int)):
            return {"status": "INVALID_TIMESTAMP", "verified": False}
        timestamp_text = str(timestamp)
        if not timestamp_text.isascii() or not timestamp_text.isdigit() or len(timestamp_text) > 12:
            return {"status": "INVALID_TIMESTAMP", "verified": False}
        timestamp_value = int(timestamp_text)
        current = time.time() if now is None else now
        if not isinstance(current, (int, float)) or isinstance(current, bool) or not math.isfinite(current):
            return {"status": "INVALID_CLOCK", "verified": False}
        if timestamp_value > current + self.max_future_skew_seconds:
            return {"status": "TIMESTAMP_IN_FUTURE", "verified": False}
        if current - timestamp_value > self.max_age_seconds:
            return {"status": "TIMESTAMP_EXPIRED", "verified": False}
        try:
            secret = self.secret_resolver(provider_id)
        except Exception as exc:
            return {"status": "SECRET_LOOKUP_FAILED", "verified": False, "error_type": type(exc).__name__}
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            secret_bytes = secret
        else:
            secret_bytes = b""
        if not secret_bytes:
            return {"status": "SECRET_UNAVAILABLE", "verified": False}
        expected = self.signature_for(secret_bytes, timestamp_text, raw_body)
        if not hmac.compare_digest(expected, supplied.casefold()):
            return {"status": "SIGNATURE_MISMATCH", "verified": False}
        replay_key = hashlib.sha256(
            provider_id.encode("utf-8") + b"\0" + timestamp_text.encode("ascii") + b"\0" + supplied.casefold().encode("ascii")
        ).hexdigest()
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._lock, interprocess_file_lock(lock_path):
            self._reload()
            if replay_key in self._data["seen"]:
                return {"status": "WEBHOOK_REPLAY", "verified": False, "replay": True}
            cutoff = int(current) - self.max_age_seconds - self.max_future_skew_seconds
            self._data["seen"] = {
                key: record for key, record in self._data["seen"].items()
                if record["timestamp"] >= cutoff
            }
            if len(self._data["seen"]) >= self.max_replay_entries:
                return {"status": "REPLAY_LEDGER_FULL", "verified": False}
            self._data["seen"][replay_key] = {
                "provider_id": provider_id,
                "timestamp": timestamp_value,
            }
            try:
                self._save()
            except Exception as exc:
                self._data["seen"].pop(replay_key, None)
                return {"status": "REPLAY_LEDGER_WRITE_FAILED", "verified": False, "error_type": type(exc).__name__}
        return {"status": "VERIFIED", "verified": True, "replay": False}
