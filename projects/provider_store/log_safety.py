"""Safe logging helpers for provider-related application code.

Use get_provider_logger(__name__) instead of the stdlib logger directly.
Records are scrubbed before handlers see them; arbitrary objects are never
serialized with repr(), and exception tracebacks are scrubbed before output.
This is a defense-in-depth layer, not permission to pass secrets to log calls.
"""
from __future__ import annotations

import json
import logging
import math
import re
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TextIO


_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|"
    r"secret|password|passwd|credential|authorization|proxy[_-]?authorization|"
    r"cookie|session|private[_-]?key)(?:$|[_-])"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|credential|client[_-]?secret|private[_-]?key|cookie|session[_-]?token)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)"""
)
_AUTH_ASSIGNMENT_RE = re.compile(
    r"""(?i)(\b(?:authorization|proxy-authorization)\b["']?\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)"""
)
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)\b(?:hf_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{12,})\b"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_URL_PASSWORD_RE = re.compile(r"(?i)(https?://[^/@:\s]+:)[^/@\s]+@")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_MAX_DEPTH = 12
_MAX_ITEMS = 200
_ROOT_LOGGER = "ai_creator_agent.provider_store"
_LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
_SENSITIVE_IDENTIFIERS = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|credential|authorization|cookie|private[_-]?key)"
)


def redact_text(value: str) -> str:
    """Redact common credential assignments, auth headers, and token formats."""
    if not isinstance(value, str):
        raise TypeError("redact_text requires a string")
    value = _PRIVATE_KEY_RE.sub("[REDACTED_PRIVATE_KEY]", value)
    value = _URL_PASSWORD_RE.sub(r"\\1[REDACTED]@", value)
    value = _AUTH_ASSIGNMENT_RE.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _SENSITIVE_ASSIGNMENT_RE.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _KNOWN_TOKEN_RE.sub("[REDACTED_TOKEN]", value)
    value = _JWT_RE.sub("[REDACTED_JWT]", value)
    return value


def _safe_exception_text(exc_info: Any) -> str:
    """Render exception metadata without exposing traceback source lines or paths."""
    try:
        exc_type, exc_value, _ = exc_info
        if exc_value is None:
            return redact_text(getattr(exc_type, "__name__", "Exception"))
        sections: list[str] = []
        current = exc_value
        seen: set[int] = set()
        for _ in range(5):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            current_type = redact_text(type(current).__name__)
            try:
                current_message = redact_text(str(current))
            except Exception:
                current_message = "[UNAVAILABLE_EXCEPTION_MESSAGE]"
            sections.append(f"{current_type}: {current_message}")
            frames = traceback.extract_tb(current.__traceback__, limit=20) if current.__traceback__ else []
            for frame in frames[-20:]:
                sections.append(f"  at {redact_text(frame.name)}:{int(frame.lineno)}")
            current = current.__cause__
            if current is None and not getattr(exc_value, "__suppress_context__", False):
                current = exc_value.__context__
        return "\\n".join(sections) or "[REDACTED_EXCEPTION]"
    except Exception:
        return "[REDACTED_EXCEPTION]"


def _sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        key = str(key)
    normalized = key.strip().lower().replace(" ", "_")
    return bool(_SENSITIVE_KEY_RE.search(normalized))


def redact_value(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    """Return a safe copy of structured data without mutating the source."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[REDACTED_BYTES]"

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "[CYCLE]"

    if isinstance(value, Mapping):
        seen.add(identity)
        result: dict[str, Any] = {}
        try:
            for index, (key, item) in enumerate(value.items()):
                if index >= _MAX_ITEMS:
                    result["[TRUNCATED]"] = True
                    break
                safe_key = str(key)
                result[safe_key] = "[REDACTED]" if _sensitive_key(safe_key) else redact_value(
                    item, _depth=_depth + 1, _seen=seen
                )
        finally:
            seen.remove(identity)
        return result

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(identity)
        result_list: list[Any] = []
        try:
            for index, item in enumerate(value):
                if index >= _MAX_ITEMS:
                    result_list.append("[TRUNCATED]")
                    break
                result_list.append(redact_value(item, _depth=_depth + 1, _seen=seen))
        finally:
            seen.remove(identity)
        return result_list

    # Avoid arbitrary __str__/__repr__: it may expose headers, tokens, or payloads.
    return f"<{type(value).__name__}>"


class SecretRedactionFilter(logging.Filter):
    """Scrub a LogRecord in place before it reaches any logger handler."""

    _STANDARD_FIELDS = set(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    ) | {"message", "asctime"}

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "[UNFORMATTABLE_LOG_MESSAGE]"
        record.msg = redact_text(message)
        record.args = ()

        if record.exc_info:
            try:
                rendered_exception = _safe_exception_text(record.exc_info)
                record.exc_text = redact_text(rendered_exception)
            except Exception:
                record.exc_text = "[REDACTED_EXCEPTION]"
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(str(record.exc_text))

        if record.stack_info:
            record.stack_info = redact_text(str(record.stack_info))

        for key, value in tuple(record.__dict__.items()):
            if key not in self._STANDARD_FIELDS:
                record.__dict__[key] = redact_value(value)
        return True


class JsonLogFormatter(logging.Formatter):
    """Compact JSON formatter that independently redacts fields and exceptions."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = redact_text(record.getMessage())
        except Exception:
            message = "[UNFORMATTABLE_LOG_MESSAGE]"
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": redact_text(str(record.levelname)),
            "logger": redact_text(str(record.name)),
            "message": message,
        }

        exception_text = record.exc_text
        if record.exc_info:
            try:
                exception_text = _safe_exception_text(record.exc_info)
            except Exception:
                exception_text = "[REDACTED_EXCEPTION]"
        if exception_text:
            payload["exception"] = redact_text(str(exception_text))
        if record.stack_info:
            payload["stack"] = redact_text(str(record.stack_info))

        for key, value in record.__dict__.items():
            if key not in SecretRedactionFilter._STANDARD_FIELDS and key not in payload:
                payload[str(key)] = redact_value(value)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def get_provider_logger(component: str = "application") -> logging.Logger:
    """Return a namespaced logger with redaction installed at the source."""
    if (
        not isinstance(component, str)
        or not component
        or len(component) > 128
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", component)
    ):
        raise ValueError("invalid provider logger component")
    logger = logging.getLogger(f"{_ROOT_LOGGER}.{component}")
    if not any(isinstance(item, SecretRedactionFilter) for item in logger.filters):
        logger.addFilter(SecretRedactionFilter())
    return logger


def configure_provider_logging(
    component: str = "application",
    *,
    stream: TextIO | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure one JSON stream handler; repeated calls replace only owned handlers."""
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 100:
        raise ValueError("invalid logging level")
    logger = get_provider_logger(component)
    logger.setLevel(level)
    for handler in tuple(logger.handlers):
        if getattr(handler, "_provider_store_managed", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.StreamHandler(stream)
    handler._provider_store_managed = True  # type: ignore[attr-defined]
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
