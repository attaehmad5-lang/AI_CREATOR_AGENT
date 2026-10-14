"""Validated request/trace context using the W3C Trace Context v00 format."""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SPAN_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})\Z"
)


class TraceContextError(ValueError):
    """Raised when an incoming request/trace context is malformed or inconsistent."""


def _valid_nonzero_hex(value: str, pattern: re.Pattern[str]) -> bool:
    return pattern.fullmatch(value) is not None and any(char != "0" for char in value)


@dataclass(frozen=True)
class TraceContext:
    """Immutable correlation context for one provider execution attempt.

    The caller's traceparent span becomes parent_span_id; a fresh span ID is
    generated for this service's outbound adapter call. No credentials or payload
    data are included in this object.
    """

    request_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    trace_flags: str = "01"

    @classmethod
    def create(
        cls,
        request_id: Any,
        *,
        trace_id: Any = None,
        traceparent: Any = None,
    ) -> "TraceContext":
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise TraceContextError("invalid request ID")
        if trace_id is not None and (
            not isinstance(trace_id, str)
            or not _valid_nonzero_hex(trace_id, _TRACE_ID_RE)
        ):
            raise TraceContextError("invalid trace ID")

        parent_span_id = None
        trace_flags = "01"
        parent_trace_id = None
        if traceparent is not None:
            if not isinstance(traceparent, str):
                raise TraceContextError("invalid traceparent")
            match = _TRACEPARENT_RE.fullmatch(traceparent)
            if match is None or match.group("version") != "00":
                raise TraceContextError("invalid traceparent")
            parent_trace_id = match.group("trace_id")
            parent_span_id = match.group("span_id")
            trace_flags = match.group("flags")
            if (
                not _valid_nonzero_hex(parent_trace_id, _TRACE_ID_RE)
                or not _valid_nonzero_hex(parent_span_id, _SPAN_ID_RE)
            ):
                raise TraceContextError("invalid traceparent")
            if trace_id is not None and trace_id != parent_trace_id:
                raise TraceContextError("trace ID conflicts with traceparent")

        chosen_trace_id = trace_id or parent_trace_id or uuid.uuid4().hex
        span_id = secrets.token_hex(8)
        # A generated span must never be all-zero, even under a faulty RNG stub.
        if not _valid_nonzero_hex(span_id, _SPAN_ID_RE):
            raise TraceContextError("could not generate a valid span ID")
        return cls(
            request_id=request_id,
            trace_id=chosen_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            trace_flags=trace_flags,
        )

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @property
    def headers(self) -> dict[str, str]:
        """Return safe propagation headers for a downstream provider adapter."""
        return {
            "traceparent": self.traceparent,
            "x-request-id": self.request_id,
        }

    def as_dict(self) -> dict[str, str | None]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "traceparent": self.traceparent,
        }
