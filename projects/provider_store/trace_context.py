"""Validated request correlation and W3C Trace Context v00 propagation."""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_SPAN_ID_RE = re.compile(r"[0-9a-f]{16}\Z")
_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})\Z"
)


class TraceContextError(ValueError):
    """Raised when incoming request or trace context is invalid."""


def _valid_nonzero_hex(value: Any, pattern: re.Pattern[str]) -> bool:
    return (
        isinstance(value, str)
        and pattern.fullmatch(value) is not None
        and any(char != "0" for char in value)
    )


@dataclass(frozen=True)
class TraceContext:
    """Immutable trace identifiers for one provider execution span.

    An incoming traceparent's span becomes parent_span_id. This service generates
    a new child span and never trusts caller-supplied outbound trace headers.
    """

    request_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    trace_flags: str = "00"

    @classmethod
    def create(
        cls,
        request_id: Any = None,
        *,
        trace_id: Any = None,
        traceparent: Any = None,
    ) -> "TraceContext":
        if request_id is None:
            request_id = "req-" + uuid.uuid4().hex
        if not isinstance(request_id, str) or _REQUEST_ID_RE.fullmatch(request_id) is None:
            raise TraceContextError("invalid request ID")
        if trace_id is not None and not _valid_nonzero_hex(trace_id, _TRACE_ID_RE):
            raise TraceContextError("invalid trace ID")

        parent_span_id = None
        parent_trace_id = None
        trace_flags = "00"
        if traceparent is not None:
            if not isinstance(traceparent, str):
                raise TraceContextError("invalid traceparent")
            match = _TRACEPARENT_RE.fullmatch(traceparent)
            if match is None or match.group("version") != "00":
                raise TraceContextError("unsupported or malformed traceparent")
            parent_trace_id = match.group("trace_id")
            parent_span_id = match.group("span_id")
            trace_flags = match.group("flags")
            if (
                not _valid_nonzero_hex(parent_trace_id, _TRACE_ID_RE)
                or not _valid_nonzero_hex(parent_span_id, _SPAN_ID_RE)
            ):
                raise TraceContextError("zero or malformed trace identifier")
            if trace_id is not None and trace_id != parent_trace_id:
                raise TraceContextError("trace ID conflicts with traceparent")

        chosen_trace_id = trace_id or parent_trace_id or uuid.uuid4().hex
        if not _valid_nonzero_hex(chosen_trace_id, _TRACE_ID_RE):
            raise TraceContextError("could not generate a valid trace ID")
        span_id = secrets.token_hex(8)
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
        """Safe outbound correlation headers; no auth or payload fields."""
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
            "trace_flags": self.trace_flags,
        }
