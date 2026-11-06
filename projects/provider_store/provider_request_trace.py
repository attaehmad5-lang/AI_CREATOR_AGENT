"""W3C trace-context propagation and unique provider request identifiers."""
from __future__ import annotations

import contextvars
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, Iterator


_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_TRACE_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
_SPAN_ID_RE = re.compile(r"[0-9a-fA-F]{16}")
_TRACEPARENT_RE = re.compile(r"00-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})")


@dataclass(frozen=True)
class ProviderRequestContext:
    request_id: str
    trace_id: str
    span_id: str
    trace_flags: str = "01"
    parent_span_id: str | None = None

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_flags": self.trace_flags,
            "traceparent": self.traceparent,
        }


_CURRENT_CONTEXT: contextvars.ContextVar[ProviderRequestContext | None] = (
    contextvars.ContextVar("provider_request_context", default=None)
)


def generate_request_id() -> str:
    """Generate a globally unique, opaque UUIDv4 request/idempotency identifier."""
    return str(uuid.uuid4())


def validate_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def parse_traceparent(value: Any) -> tuple[str, str, str]:
    """Parse W3C Trace Context version 00; reject malformed and all-zero IDs."""
    if not isinstance(value, str) or len(value) != 55:
        raise ValueError("INVALID_TRACEPARENT")
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None:
        raise ValueError("INVALID_TRACEPARENT")
    trace_id, parent_span_id, flags = (part.casefold() for part in match.groups())
    if trace_id == "0" * 32 or parent_span_id == "0" * 16:
        raise ValueError("INVALID_TRACEPARENT")
    return trace_id, parent_span_id, flags


def create_request_context(
    *, request_id: str | None = None,
    trace_id: str | None = None,
    traceparent: str | None = None,
    trace_flags: str = "01",
) -> ProviderRequestContext:
    """Create a request context, continuing a valid remote trace when supplied."""
    if request_id is None:
        request_id = generate_request_id()
    if not validate_request_id(request_id):
        raise ValueError("INVALID_REQUEST_ID")

    remote_parent = None
    if traceparent is not None:
        remote_trace, remote_parent, remote_flags = parse_traceparent(traceparent)
        if trace_id is not None and (
            not isinstance(trace_id, str) or trace_id.casefold() != remote_trace
        ):
            raise ValueError("TRACE_ID_CONFLICT")
        trace_id = remote_trace
        trace_flags = remote_flags

    if trace_id is None:
        trace_id = secrets.token_hex(16)
    if not isinstance(trace_id, str) or _TRACE_ID_RE.fullmatch(trace_id) is None:
        raise ValueError("INVALID_TRACE_ID")
    trace_id = trace_id.casefold()
    if trace_id == "0" * 32:
        raise ValueError("INVALID_TRACE_ID")
    if not isinstance(trace_flags, str) or re.fullmatch(r"[0-9a-fA-F]{2}", trace_flags) is None:
        raise ValueError("INVALID_TRACE_FLAGS")
    span_id = secrets.token_hex(8)
    while span_id == "0" * 16:
        span_id = secrets.token_hex(8)
    return ProviderRequestContext(
        request_id=request_id,
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=trace_flags.casefold(),
        parent_span_id=remote_parent,
    )


@contextmanager
def activate_request_context(context: ProviderRequestContext) -> Iterator[ProviderRequestContext]:
    if not isinstance(context, ProviderRequestContext):
        raise ValueError("INVALID_REQUEST_CONTEXT")
    token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_CONTEXT.reset(token)


def current_request_context() -> ProviderRequestContext | None:
    return _CURRENT_CONTEXT.get()


def current_trace_context() -> dict[str, str | None] | None:
    context = current_request_context()
    return context.as_dict() if context is not None else None


@contextmanager
def child_span() -> Iterator[ProviderRequestContext]:
    """Create a child span within the active trace and restore its parent on exit."""
    parent = current_request_context()
    if parent is None:
        raise RuntimeError("NO_ACTIVE_PROVIDER_TRACE")
    span_id = secrets.token_hex(8)
    while span_id == "0" * 16 or span_id == parent.span_id:
        span_id = secrets.token_hex(8)
    child = ProviderRequestContext(
        request_id=parent.request_id,
        trace_id=parent.trace_id,
        span_id=span_id,
        trace_flags=parent.trace_flags,
        parent_span_id=parent.span_id,
    )
    with activate_request_context(child):
        yield child


def inject_trace_headers(headers: Any = None) -> dict[str, str]:
    """Return a copied header mapping with the active W3C traceparent injected."""
    if headers is None:
        result: dict[str, str] = {}
    elif isinstance(headers, dict):
        result = dict(headers)
    else:
        raise ValueError("INVALID_TRACE_HEADERS")
    context = current_request_context()
    if context is not None:
        for key in list(result):
            if isinstance(key, str) and key.casefold() == "traceparent":
                result.pop(key)
        result["traceparent"] = context.traceparent
    return result


def extract_trace_context(headers: Any) -> ProviderRequestContext:
    """Extract traceparent from a case-insensitive HTTP header mapping."""
    if not isinstance(headers, dict):
        raise ValueError("INVALID_TRACE_HEADERS")
    value = next(
        (item for key, item in headers.items() if isinstance(key, str) and key.casefold() == "traceparent"),
        None,
    )
    if value is None:
        raise ValueError("TRACEPARENT_REQUIRED")
    return create_request_context(traceparent=value)


def trace_provider_request(function: Any) -> Any:
    """Decorator that scopes trace context and adds IDs to every service response."""
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        request_id = kwargs.get("request_id")
        trace_id = kwargs.pop("trace_id", None)
        traceparent = kwargs.pop("traceparent", None)
        try:
            context = create_request_context(
                request_id=request_id, trace_id=trace_id, traceparent=traceparent,
            )
        except (TypeError, ValueError) as exc:
            status = str(exc) if str(exc).startswith("INVALID_") or str(exc) == "TRACE_ID_CONFLICT" else "INVALID_REQUEST_CONTEXT"
            return {"status": status, "executed": False}
        kwargs["request_id"] = context.request_id
        # These are explicit service-level fields consumed by the method and
        # its **route_options; the routing manager must not receive them.
        kwargs["trace_id"] = context.trace_id
        kwargs["traceparent"] = context.traceparent
        with activate_request_context(context):
            result = function(*args, **kwargs)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("request_id", context.request_id)
            result.setdefault("trace_id", context.trace_id)
            result.setdefault("traceparent", context.traceparent)
        return result
    return wrapped
