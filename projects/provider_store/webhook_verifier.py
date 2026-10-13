"""Bounded HMAC webhook verification with timestamp and replay protection."""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import threading
import time
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")
_SIGNATURE = re.compile(r"sha256=[0-9a-fA-F]{64}")


class ProviderWebhookVerifier:
    """Verify signed callbacks without logging or returning request bodies or secrets.

    The resolver must read provider secrets from an approved secret store/environment.
    Replay protection is process-local; multi-worker deployments need a shared replay store.
    """

    def __init__(
        self,
        secret_resolver: Any,
        *,
        max_body_bytes: int = 1_000_000,
        timestamp_tolerance_seconds: int = 300,
        max_replay_entries: int = 10_000,
        clock: Any = time.time,
    ) -> None:
        if not callable(secret_resolver):
            raise ValueError("secret_resolver must be callable")
        if (
            not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool)
            or max_body_bytes < 1 or max_body_bytes > 100_000_000
        ):
            raise ValueError("max_body_bytes is out of range")
        if (
            not isinstance(timestamp_tolerance_seconds, int)
            or isinstance(timestamp_tolerance_seconds, bool)
            or timestamp_tolerance_seconds < 1 or timestamp_tolerance_seconds > 86400
        ):
            raise ValueError("timestamp tolerance is out of range")
        if (
            not isinstance(max_replay_entries, int) or isinstance(max_replay_entries, bool)
            or max_replay_entries < 1 or max_replay_entries > 1_000_000
        ):
            raise ValueError("max_replay_entries is out of range")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.secret_resolver = secret_resolver
        self.max_body_bytes = max_body_bytes
        self.timestamp_tolerance_seconds = timestamp_tolerance_seconds
        self.max_replay_entries = max_replay_entries
        self.clock = clock
        self._lock = threading.RLock()
        self._seen: dict[str, float] = {}

    def verify(
        self,
        *,
        provider_id: str,
        body: bytes,
        timestamp: str,
        signature: str,
        event_id: str,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"verified": False, "status": "INVALID_PROVIDER_ID"}
        if not isinstance(body, bytes):
            return {"verified": False, "status": "INVALID_WEBHOOK_BODY"}
        if len(body) > self.max_body_bytes:
            return {"verified": False, "status": "WEBHOOK_BODY_TOO_LARGE"}
        if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp):
            return {"verified": False, "status": "INVALID_WEBHOOK_TIMESTAMP"}
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            return {"verified": False, "status": "INVALID_WEBHOOK_SIGNATURE"}
        if not isinstance(event_id, str) or not _IDENTIFIER.fullmatch(event_id):
            return {"verified": False, "status": "INVALID_WEBHOOK_EVENT_ID"}
        try:
            now = self.clock()
        except Exception:
            return {"verified": False, "status": "WEBHOOK_CLOCK_FAILED"}
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            return {"verified": False, "status": "WEBHOOK_CLOCK_FAILED"}
        event_time = int(timestamp)
        if abs(float(now) - event_time) > self.timestamp_tolerance_seconds:
            return {"verified": False, "status": "WEBHOOK_TIMESTAMP_OUT_OF_RANGE"}
        try:
            secret = self.secret_resolver(provider_id)
        except Exception:
            return {"verified": False, "status": "WEBHOOK_SECRET_LOOKUP_FAILED"}
        if isinstance(secret, str):
            secret_bytes = secret.encode("utf-8")
        elif isinstance(secret, bytes):
            secret_bytes = secret
        else:
            secret_bytes = b""
        if len(secret_bytes) < 16 or len(secret_bytes) > 4096:
            return {"verified": False, "status": "WEBHOOK_SECRET_UNAVAILABLE"}

        expected = "sha256=" + hmac.new(
            secret_bytes, timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected.casefold(), signature.casefold()):
            return {"verified": False, "status": "WEBHOOK_SIGNATURE_INVALID"}

        replay_key = provider_id + ":" + event_id
        with self._lock:
            cutoff = float(now) - self.timestamp_tolerance_seconds
            self._seen = {key: seen_at for key, seen_at in self._seen.items() if seen_at >= cutoff}
            if replay_key in self._seen:
                return {"verified": False, "status": "WEBHOOK_REPLAY_DETECTED"}
            if len(self._seen) >= self.max_replay_entries:
                return {"verified": False, "status": "WEBHOOK_REPLAY_CACHE_FULL"}
            self._seen[replay_key] = float(now)
        return {
            "verified": True,
            "status": "WEBHOOK_VERIFIED",
            "provider": provider_id,
            "event_id": event_id,
            "timestamp": event_time,
        }
