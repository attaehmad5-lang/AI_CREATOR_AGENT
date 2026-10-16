"""Bounded HMAC webhook verification with timestamp and replay protection."""
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
_SIGNATURE_RE = re.compile(r"sha256=[0-9a-f]{64}\Z")


class ProviderWebhookVerifier:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_skew_seconds: int = 300,
        max_payload_bytes: int = 1_000_000,
        clock: Any = None,
    ) -> None:
        if isinstance(max_skew_seconds, bool) or not isinstance(max_skew_seconds, int) or not 1 <= max_skew_seconds <= 3600:
            raise ValueError("max_skew_seconds must be within 1..3600")
        if isinstance(max_payload_bytes, bool) or not isinstance(max_payload_bytes, int) or not 1 <= max_payload_bytes <= 10_000_000:
            raise ValueError("max_payload_bytes must be within 1..10000000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_skew_seconds = max_skew_seconds
        self.max_payload_bytes = max_payload_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "events": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "events": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid webhook replay state JSON") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("events"), dict):
            raise ValueError("invalid webhook replay state schema")
        for event_key, event in loaded["events"].items():
            if (
                not isinstance(event_key, str) or not re.fullmatch(r"[0-9a-f]{64}", event_key)
                or not isinstance(event, dict)
                or not isinstance(event.get("provider_id"), str)
                or not isinstance(event.get("received_at"), str)
                or not isinstance(event.get("payload_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", event["payload_sha256"])
            ):
                raise ValueError("invalid webhook replay record")
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

    def verify(
        self,
        *,
        provider_id: str,
        payload: bytes,
        signature: str,
        timestamp: int | str,
        event_id: str,
        secret: str,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"verified": False, "status": "INVALID_PROVIDER_ID"}
        if not isinstance(event_id, str) or not _ID_RE.fullmatch(event_id):
            return {"verified": False, "status": "INVALID_EVENT_ID"}
        if not isinstance(payload, bytes):
            return {"verified": False, "status": "INVALID_WEBHOOK_PAYLOAD"}
        if len(payload) > self.max_payload_bytes:
            return {"verified": False, "status": "WEBHOOK_PAYLOAD_TOO_LARGE"}
        if not isinstance(secret, str) or len(secret) < 16 or len(secret) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            return {"verified": False, "status": "WEBHOOK_SECRET_INVALID"}
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, str)) or not re.fullmatch(r"\d{1,12}", str(timestamp)):
            return {"verified": False, "status": "INVALID_WEBHOOK_TIMESTAMP"}
        timestamp_value = int(timestamp)
        now_dt = self.clock().astimezone(timezone.utc)
        now = int(now_dt.timestamp())
        if abs(now - timestamp_value) > self.max_skew_seconds:
            return {"verified": False, "status": "WEBHOOK_TIMESTAMP_OUT_OF_RANGE"}
        if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
            return {"verified": False, "status": "INVALID_WEBHOOK_SIGNATURE_FORMAT"}
        signed = str(timestamp_value).encode("ascii") + b"." + payload
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return {"verified": False, "status": "WEBHOOK_SIGNATURE_INVALID"}

        event_key = hashlib.sha256(f"{provider_id}\0{event_id}".encode("utf-8")).hexdigest()
        now_iso = now_dt.isoformat()
        payload_hash = hashlib.sha256(payload).hexdigest()
        with self._transaction():
            cutoff = now - (self.max_skew_seconds * 2)
            for key, record in list(self.data["events"].items()):
                try:
                    received = datetime.fromisoformat(record["received_at"]).astimezone(timezone.utc)
                except (ValueError, TypeError, KeyError):
                    raise ValueError("invalid webhook replay timestamp")
                if received.timestamp() < cutoff:
                    del self.data["events"][key]
            if event_key in self.data["events"]:
                self._save()
                return {"verified": False, "status": "WEBHOOK_REPLAY_REJECTED", "provider": provider_id}
            self.data["events"][event_key] = {
                "provider_id": provider_id,
                "received_at": now_iso,
                "payload_sha256": payload_hash,
            }
            self._save()
        return {
            "verified": True,
            "status": "WEBHOOK_VERIFIED",
            "provider": provider_id,
            "event_id": event_id,
            "payload_sha256": payload_hash,
        }
