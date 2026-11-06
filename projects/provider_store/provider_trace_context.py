"""Request identity and W3C Trace Context propagation for provider operations."""
from __future__ import annotations

import contextvars
import re
import secrets
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})\Z"
)
_CURRENT_TRACE_CONTEXT: contextvars.ContextVar["ProviderRequestTraceContext | None"] = (
    contextvars.ContextVar("provider_request_trace_context", default=None)
)


@dataclass(frozen=True)
class ProviderRequestTraceContext:
    """Immutable request correlation IDs and the current W3C traceparent span."""

    request_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    trace_flags: str = "00"

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    @classmethod
    def create(
        cls,
        *,
        request_id: str | None = None,
        traceparent: str | None = None,
    ) -> "ProviderRequestTraceContext":
        if request_id is None:
            request_id = secrets.token_hex(16)
        if (
            not isinstance(request_id, str)
            or _REQUEST_ID_RE.fullmatch(request_id) is None
        ):
            raise ValueError("INVALID_REQUEST_ID")

        current = _CURRENT_TRACE_CONTEXT.get()
        if traceparent is None and current is not None:
            return current.child(request_id=request_id)

        if traceparent is None:
            trace_id = secrets.token_hex(16)
            flags = "00"
            parent_span_id = None
        else:
            if not isinstance(traceparent, str) or len(traceparent) != 55:
                raise ValueError("INVALID_TRACEPARENT")
            match = _TRACEPARENT_RE.fullmatch(traceparent)
            if match is None or match.group("version") != "00":
                raise ValueError("INVALID_TRACEPARENT")
            trace_id = match.group("trace_id")
            parent_span_id = match.group("span_id")
            flags = match.group("flags")
            if trace_id == "0" * 32 or parent_span_id == "0" * 16:
                raise ValueError("INVALID_TRACEPARENT")
        span_id = secrets.token_hex(8)
        return cls(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            trace_flags=flags,
        )

    def child(self, *, request_id: str | None = None) -> "ProviderRequestTraceContext":
        identity = self.request_id if request_id is None else request_id
        if not isinstance(identity, str) or _REQUEST_ID_RE.fullmatch(identity) is None:
            raise ValueError("INVALID_REQUEST_ID")
        return ProviderRequestTraceContext(
            request_id=identity,
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=self.span_id,
            trace_flags=self.trace_flags,
        )


def current_provider_trace_context() -> ProviderRequestTraceContext | None:
    """Return the active synchronous request context, if one is established."""
    return _CURRENT_TRACE_CONTEXT.get()


def with_provider_trace_context(method: Callable[..., Any]) -> Callable[..., Any]:
    """Create/validate a request trace, propagate it, and enrich all dictionary results.

    Nested provider service operations create child spans under the existing trace.
    The incoming traceparent is treated as untrusted data and must be valid W3C
    version 00 context; malformed context fails before the wrapped operation runs.
    """
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        request_id = kwargs.get("request_id")
        traceparent = kwargs.get("traceparent")
        try:
            context = ProviderRequestTraceContext.create(
                request_id=request_id, traceparent=traceparent,
            )
        except ValueError as exc:
            status = str(exc) if str(exc) in {"INVALID_REQUEST_ID", "INVALID_TRACEPARENT"} else "INVALID_TRACE_CONTEXT"
            return {
                "status": status,
                "allowed": False,
                "executed": False,
                "authenticated": False,
                "request_id": request_id if isinstance(request_id, str) else None,
            }

        kwargs["request_id"] = context.request_id
        kwargs["_trace_context"] = context
        token = _CURRENT_TRACE_CONTEXT.set(context)
        try:
            result = method(self, *args, **kwargs)
        finally:
            _CURRENT_TRACE_CONTEXT.reset(token)
        if isinstance(result, dict):
            # Preserve operation-specific request IDs if a downstream manager
            # reports a conflict, but always provide the established correlation.
            result.setdefault("request_id", context.request_id)
            result["trace_id"] = context.trace_id
            result["span_id"] = context.span_id
            result["traceparent"] = context.traceparent
            if context.parent_span_id is not None:
                result["parent_span_id"] = context.parent_span_id
        return result

    return wrapped
