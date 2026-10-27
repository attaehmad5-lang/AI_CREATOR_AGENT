"""Bounded W3C trace-context propagation and secret-safe provider spans."""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from typing import Any

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_SENSITIVE_KEYS = ("token", "secret", "password", "api_key", "credential", "authorization", "prompt", "payload", "content")
_MAX_NAME = 96
_MAX_ATTRIBUTES = 24
_MAX_VALUE = 256


def parse_traceparent(value: Any) -> dict[str, Any]:
    """Parse W3C traceparent version 00; reject malformed/all-zero identifiers."""
    if not isinstance(value, str) or len(value) != 55:
        return {"status": "INVALID_TRACEPARENT"}
    match = _TRACEPARENT.fullmatch(value)
    if not match:
        return {"status": "INVALID_TRACEPARENT"}
    trace_id, parent_span_id, flags = match.groups()
    if trace_id == "0" * 32 or parent_span_id == "0" * 16:
        return {"status": "INVALID_TRACEPARENT"}
    return {
        "status": "VALID",
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
        "flags": flags,
    }


def _safe_attributes(attributes: Any) -> dict[str, Any]:
    if not isinstance(attributes, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, value in list(attributes.items())[:_MAX_ATTRIBUTES]:
        if not isinstance(key, str) or not key or len(key) > 80:
            continue
        lowered = key.casefold()
        if any(token in lowered for token in _SENSITIVE_KEYS):
            safe[key[:80]] = "[REDACTED]"
        elif isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:_MAX_VALUE]
        else:
            safe[key] = "[OMITTED]"
    return safe


class ProviderTraceManager:
    """In-process bounded span recorder with W3C traceparent propagation.

    This is a tracing primitive, not a replacement for an external distributed
    tracing backend. It deliberately never records prompts, payloads or secrets.
    """

    def __init__(self, *, max_spans: int = 2000, clock: Any = time.monotonic) -> None:
        if isinstance(max_spans, bool) or not isinstance(max_spans, int) or not 1 <= max_spans <= 100000:
            raise ValueError("max_spans must be an integer between 1 and 100000")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.max_spans = max_spans
        self.clock = clock
        self._lock = threading.RLock()
        self._spans: deque[dict[str, Any]] = deque(maxlen=max_spans)

    def start_span(
        self,
        name: str,
        *,
        traceparent: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME:
            return {"status": "INVALID_SPAN_NAME"}
        parent: dict[str, Any] | None = None
        if traceparent is not None:
            parsed = parse_traceparent(traceparent)
            if parsed["status"] != "VALID":
                return {"status": "INVALID_TRACEPARENT"}
            parent = parsed
        trace_id = parent["trace_id"] if parent else secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        flags = parent["flags"] if parent else "01"
        started = self.clock()
        if isinstance(started, bool) or not isinstance(started, (int, float)):
            return {"status": "INVALID_CLOCK"}
        return {
            "status": "SPAN_STARTED",
            "name": name.strip(),
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent["parent_span_id"] if parent else None,
            "traceparent": f"00-{trace_id}-{span_id}-{flags}",
            "started_at": float(started),
            "attributes": _safe_attributes(attributes),
        }

    def finish_span(self, span: Any, *, outcome: str = "OK", duration_ms: float | None = None) -> dict[str, Any]:
        if not isinstance(span, dict) or span.get("status") != "SPAN_STARTED":
            return {"status": "INVALID_SPAN"}
        if not isinstance(outcome, str) or outcome not in {"OK", "ERROR", "CANCELLED"}:
            return {"status": "INVALID_SPAN_OUTCOME"}
        if duration_ms is None:
            ended = self.clock()
            if isinstance(ended, bool) or not isinstance(ended, (int, float)) or ended < span["started_at"]:
                return {"status": "INVALID_CLOCK"}
            duration = (float(ended) - span["started_at"]) * 1000.0
        else:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) or duration_ms < 0:
                return {"status": "INVALID_DURATION"}
            duration = float(duration_ms)
        record = {
            "name": span["name"],
            "trace_id": span["trace_id"],
            "span_id": span["span_id"],
            "parent_span_id": span["parent_span_id"],
            "outcome": outcome,
            "duration_ms": min(duration, 86_400_000.0),
            "attributes": _safe_attributes(span.get("attributes")),
        }
        with self._lock:
            self._spans.append(record)
        return {"status": "SPAN_RECORDED", "span": dict(record)}

    def snapshot(self, *, trace_id: str | None = None) -> dict[str, Any]:
        if trace_id is not None and (
            not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
        ):
            return {"status": "INVALID_TRACE_ID", "spans": []}
        with self._lock:
            records = [dict(item) for item in self._spans if trace_id is None or item["trace_id"] == trace_id]
        return {"status": "TRACES_AVAILABLE" if records else "NO_TRACES", "count": len(records), "spans": records}
