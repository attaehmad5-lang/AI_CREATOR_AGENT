"""Secret redaction for logs, audit entries and error messages."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = ("key", "token", "secret", "password", "passwd", "authorization", "credential", "cookie")
_TEXT_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + REDACTED),
    (re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_\-]{8,}"), REDACTED),
    (
        re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;&\"']+"),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(?i)([?&](?:api[_-]?key|token|key|access_token)=)[^&\s]+"), r"\1" + REDACTED),
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"), REDACTED),
]


def redact_text(text: str) -> str:
    for pattern, repl in _TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SENSITIVE_KEYS)


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secrets removed."""
    if isinstance(obj, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _is_sensitive_key(k) and obj[k] not in (None, "") else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj
