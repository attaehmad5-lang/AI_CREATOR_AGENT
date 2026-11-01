"""Bounded, deterministic provider streaming response sessions.

This module validates stream lifecycle and chunk ordering. It does not perform
network I/O or claim that a provider supports streaming.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import Any, Callable


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")


class ProviderStreamSession:
    """Thread-safe stream accumulator with strict sequence and resource bounds."""

    def __init__(
        self,
        request_id: str,
        *,
        max_chunks: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ValueError("request_id must be a non-empty bounded string")
        if not isinstance(max_chunks, int) or isinstance(max_chunks, bool) or not 1 <= max_chunks <= 1_000_000:
            raise ValueError("max_chunks must be a positive bounded integer")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 2**31:
            raise ValueError("max_bytes must be a positive bounded integer")
        try:
            timeout_value = float(timeout_seconds)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout_seconds must be finite and positive") from None
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_value)
            or timeout_value <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self.request_id = request_id
        self.max_chunks = max_chunks
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_value
        self._clock = clock or time.monotonic
        self._created_at = self._read_clock()
        self._last_activity = self._created_at
        self._chunks: list[bytes] = []
        self._digests: list[str] = []
        self._total_bytes = 0
        self._state = "OPEN"
        self._lock = threading.RLock()

    def _read_clock(self) -> float:
        value = self._clock()
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("stream clock must return a finite number") from None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(numeric):
            raise ValueError("stream clock must return a finite number")
        return numeric

    def _expire_if_needed(self, now: float) -> bool:
        if self._state == "OPEN" and now - self._last_activity > self.timeout_seconds:
            self._state = "TIMED_OUT"
        return self._state == "TIMED_OUT"

    def append(self, *, sequence: int, chunk: bytes | str) -> dict[str, Any]:
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            return {"status": "INVALID_SEQUENCE", "accepted": False}
        if isinstance(chunk, str):
            try:
                payload = chunk.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return {"status": "INVALID_CHUNK", "accepted": False}
        elif isinstance(chunk, bytes):
            payload = chunk
        else:
            return {"status": "INVALID_CHUNK", "accepted": False}
        digest = hashlib.sha256(payload).hexdigest()
        with self._lock:
            if self._expire_if_needed(self._read_clock()):
                return {"status": "STREAM_TIMED_OUT", "accepted": False}
            if self._state != "OPEN":
                return {"status": "STREAM_NOT_OPEN", "accepted": False, "state": self._state}
            if sequence < len(self._chunks):
                if self._digests[sequence] == digest:
                    return {"status": "DUPLICATE_CHUNK", "accepted": False, "idempotent": True}
                return {"status": "SEQUENCE_CONFLICT", "accepted": False}
            if sequence != len(self._chunks):
                return {"status": "SEQUENCE_GAP", "accepted": False, "expected_sequence": len(self._chunks)}
            if len(self._chunks) >= self.max_chunks:
                self._state = "LIMIT_EXCEEDED"
                return {"status": "CHUNK_LIMIT_EXCEEDED", "accepted": False}
            if self._total_bytes + len(payload) > self.max_bytes:
                self._state = "LIMIT_EXCEEDED"
                return {"status": "BYTE_LIMIT_EXCEEDED", "accepted": False}
            self._chunks.append(payload)
            self._digests.append(digest)
            self._total_bytes += len(payload)
            self._last_activity = self._read_clock()
            return {
                "status": "CHUNK_ACCEPTED", "accepted": True,
                "sequence": sequence, "chunk_bytes": len(payload),
                "total_bytes": self._total_bytes,
            }

    def heartbeat(self) -> dict[str, Any]:
        with self._lock:
            if self._expire_if_needed(self._read_clock()):
                return {"status": "STREAM_TIMED_OUT", "state": self._state}
            if self._state != "OPEN":
                return {"status": "STREAM_NOT_OPEN", "state": self._state}
            self._last_activity = self._read_clock()
            return {"status": "HEARTBEAT_ACCEPTED", "state": self._state}

    def finish(self) -> dict[str, Any]:
        with self._lock:
            if self._expire_if_needed(self._read_clock()):
                return {"status": "STREAM_TIMED_OUT", "finished": False}
            if self._state != "OPEN":
                return {"status": "STREAM_NOT_OPEN", "finished": False, "state": self._state}
            self._state = "COMPLETED"
            payload = b"".join(self._chunks)
            return {
                "status": "STREAM_COMPLETED", "finished": True,
                "chunk_count": len(self._chunks), "total_bytes": self._total_bytes,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content": payload,
            }

    def cancel(self, *, reason: str = "USER_CANCELLED") -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            return {"status": "INVALID_CANCEL_REASON", "cancelled": False}
        with self._lock:
            if self._state != "OPEN":
                return {"status": "STREAM_NOT_OPEN", "cancelled": False, "state": self._state}
            self._state = "CANCELLED"
            self._chunks.clear()
            self._digests.clear()
            self._total_bytes = 0
            return {"status": "STREAM_CANCELLED", "cancelled": True, "reason": reason}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._expire_if_needed(self._read_clock())
            return {
                "request_id": self.request_id, "state": self._state,
                "chunk_count": len(self._chunks), "total_bytes": self._total_bytes,
                "age_seconds": max(0.0, self._read_clock() - self._created_at),
            }
