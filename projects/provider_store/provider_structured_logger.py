"""Secret-safe JSON event logging for provider lifecycle diagnostics."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_SECRET_VALUE = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{16,}\b|"
    r"\bBearer\s+\S+|\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|authorization)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def _safe_field(value: str) -> bool:
    """Reject credential-shaped values even when they fit an identifier grammar."""
    return not _SECRET_VALUE.search(value)


class ProviderStructuredLogger:
    """Emit compact JSON records using only explicitly allowed diagnostic fields."""

    def __init__(self, logger: logging.Logger | None = None, *, clock: Any = None) -> None:
        if logger is not None and not isinstance(logger, logging.Logger):
            raise ValueError("logger must be a logging.Logger")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.logger = logger or logging.getLogger("ai_creator_agent.provider_store")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def emit(
        self,
        event: str,
        *,
        provider_id: str | None = None,
        status: str,
        outcome: str,
        duration_ms: float | None = None,
        request_id: str | None = None,
        category: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            return {"status": "INVALID_LOG_EVENT", "logged": False}
        if not isinstance(status, str) or not _CODE.fullmatch(status) or not _safe_field(status):
            return {"status": "INVALID_LOG_STATUS", "logged": False}
        if not isinstance(outcome, str) or outcome not in {"OK", "ERROR", "CANCELLED"}:
            return {"status": "INVALID_LOG_OUTCOME", "logged": False}
        timestamp = self.clock()
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            return {"status": "INVALID_LOG_CLOCK", "logged": False}
        record: dict[str, Any] = {
            "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
            "event": event,
            "status": status,
            "outcome": outcome,
        }
        if isinstance(provider_id, str) and _PROVIDER.fullmatch(provider_id) and _safe_field(provider_id):
            record["provider_id"] = provider_id
        if isinstance(category, str) and _CODE.fullmatch(category) and _safe_field(category):
            record["category"] = category
        if isinstance(request_id, str) and _REQUEST.fullmatch(request_id) and _safe_field(request_id):
            record["request_id"] = request_id
        if duration_ms is not None:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
                return {"status": "INVALID_LOG_DURATION", "logged": False}
            try:
                duration = float(duration_ms)
            except (OverflowError, TypeError, ValueError):
                return {"status": "INVALID_LOG_DURATION", "logged": False}
            if not math.isfinite(duration) or duration < 0:
                return {"status": "INVALID_LOG_DURATION", "logged": False}
            record["duration_ms"] = min(duration, 86_400_000.0)
        if error_code is not None and isinstance(error_code, str) and _CODE.fullmatch(error_code) and _safe_field(error_code):
            record["error_code"] = error_code
        try:
            self.logger.log(
                logging.INFO if outcome == "OK" else logging.WARNING,
                json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            return {"status": "LOG_WRITE_FAILED", "logged": False}
        return {"status": "STRUCTURED_EVENT_LOGGED", "logged": True, "record": record}
