"""Provider-neutral webhook signature and replay verification primitives.

The shared secret must be supplied by a secret store/environment by the caller and
must never be persisted or returned by this module. Provider-specific signature
canonicalization should be implemented by an adapter when the provider differs from
the timestamp-dot-body HMAC-SHA256 convention used here.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections import OrderedDict
from typing import Any


class ProviderWebhookVerifier:
    """Verify timestamped HMAC-SHA256 callbacks and reject replayed event IDs."""

    def __init__(
        self,
        secret: str,
        *,
        max_age_seconds: int = 300,
        max_body_bytes: int = 1_000_000,
        max_replay_entries: int = 10_000,
    ) -> None:
        if not isinstance(secret, str) or len(secret) < 16 or len(secret) > 16384:
            raise ValueError("webhook secret must contain 16 to 16384 characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in secret):
            raise ValueError("webhook secret contains unsupported control characters")
        for name, value, minimum, maximum in (
            ("max_age_seconds", max_age_seconds, 1, 86400),
            ("max_body_bytes", max_body_bytes, 1, 10_000_000),
            ("max_replay_entries", max_replay_entries, 1, 1_000_000),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        self._secret = secret.encode("utf-8")
        self.max_age_seconds = max_age_seconds
        self.max_body_bytes = max_body_bytes
        self.max_replay_entries = max_replay_entries
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def sign_for_testing(secret: str, body: bytes, timestamp: int) -> str:
        """Generate a compatible signature for local tests and provider adapter development."""
        if not isinstance(secret, str) or not isinstance(body, bytes) or not isinstance(timestamp, int):
            raise ValueError("invalid signature inputs")
        message = str(timestamp).encode("ascii") + b"." + body
        digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return "sha256=" + digest

    def verify(
        self,
        body: bytes,
        *,
        signature_header: str,
        timestamp_header: str,
        event_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(body, bytes):
            return {"valid": False, "status": "INVALID_BODY"}
        if len(body) > self.max_body_bytes:
            return {"valid": False, "status": "BODY_TOO_LARGE"}
        if not isinstance(signature_header, str) or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature_header):
            return {"valid": False, "status": "INVALID_SIGNATURE_FORMAT"}
        if not isinstance(timestamp_header, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp_header):
            return {"valid": False, "status": "INVALID_TIMESTAMP"}
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", event_id):
            return {"valid": False, "status": "INVALID_EVENT_ID"}
        current = time.time() if now is None else now
        if not isinstance(current, (int, float)) or isinstance(current, bool) or current < 0:
            return {"valid": False, "status": "INVALID_CLOCK"}
        timestamp = int(timestamp_header)
        if abs(current - timestamp) > self.max_age_seconds:
            return {"valid": False, "status": "TIMESTAMP_OUT_OF_WINDOW"}

        message = timestamp_header.encode("ascii") + b"." + body
        expected = "sha256=" + hmac.new(self._secret, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.casefold(), signature_header.casefold()):
            return {"valid": False, "status": "SIGNATURE_MISMATCH"}

        with self._lock:
            if event_id in self._seen:
                return {"valid": False, "status": "REPLAY_DETECTED"}
            self._seen[event_id] = float(current)
            while len(self._seen) > self.max_replay_entries:
                self._seen.popitem(last=False)
        return {"valid": True, "status": "VERIFIED", "event_id": event_id}

    def replay_cache_size(self) -> int:
        with self._lock:
            return len(self._seen)
