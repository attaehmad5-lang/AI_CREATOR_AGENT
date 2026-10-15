"""Bounded sensitive-data detection and opt-in privacy enforcement.

Findings contain only category, safe structural path, and severity; matched values
are never returned. Redaction is explicit so callers do not silently alter prompts.
"""
from __future__ import annotations

import re
from typing import Any


class SensitiveDataScanner:
    """Scan nested Python values with depth/node bounds and no matched-value output."""

    PATTERNS = (
        ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I), "critical"),
        ("BEARER_TOKEN", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I), "critical"),
        ("CREDENTIAL_ASSIGNMENT", re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|authorization)\s*[:=]\s*[^\s,;]{4,}",
            re.I,
        ), "critical"),
        ("API_TOKEN", re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
        ), "critical"),
        ("US_SSN", re.compile(r"\b(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b"), "high"),
        ("EMAIL_ADDRESS", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I), "medium"),
        ("PHONE_NUMBER", re.compile(r"(?<!\w)(?:\+?\d[\d(). -]{7,}\d)(?!\w)"), "medium"),
    )
    SECRET_KEYS = frozenset({
        "api_key", "apikey", "access_token", "refresh_token", "client_secret",
        "password", "passwd", "secret", "authorization", "bearer", "private_key",
        "session_cookie", "cookie", "credential", "credentials",
    })
    PERSONAL_KEYS = frozenset({"email", "phone", "phone_number", "ssn", "social_security_number", "date_of_birth", "dob"})

    def __init__(self, *, max_depth: int = 32, max_nodes: int = 10000, max_string_length: int = 1_000_000, max_findings: int = 100) -> None:
        limits = (
            ("max_depth", max_depth, 1, 256),
            ("max_nodes", max_nodes, 1, 1_000_000),
            ("max_string_length", max_string_length, 1, 10_000_000),
            ("max_findings", max_findings, 1, 10000),
        )
        for name, value, minimum, maximum in limits:
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_string_length = max_string_length
        self.max_findings = max_findings

    @staticmethod
    def _safe_key(key: Any) -> str:
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
            return key
        return "[key]"

    @classmethod
    def _field_category(cls, key: Any) -> tuple[str, str] | None:
        if not isinstance(key, str):
            return None
        normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if normalized in cls.SECRET_KEYS:
            return ("CREDENTIAL_FIELD", "critical")
        if normalized in cls.PERSONAL_KEYS:
            return ("PERSONAL_DATA_FIELD", "high")
        return None

    def scan(self, value: Any) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        seen: set[int] = set()
        stack: list[tuple[Any, str, int, str | None]] = [(value, "$", 0, None)]
        visited = 0
        limited = False

        def add(category: str, path: str, severity: str) -> None:
            if len(findings) < self.max_findings:
                findings.append({"category": category, "path": path, "severity": severity})

        while stack:
            current, path, depth, inherited_category = stack.pop()
            visited += 1
            if visited > self.max_nodes or depth > self.max_depth:
                limited = True
                break
            if inherited_category:
                add(inherited_category, path, "critical" if inherited_category == "CREDENTIAL_FIELD" else "high")
                continue
            if isinstance(current, str):
                if len(current) > self.max_string_length:
                    limited = True
                    break
                for category, pattern, severity in self.PATTERNS:
                    if pattern.search(current):
                        add(category, path, severity)
                continue
            if isinstance(current, dict):
                identity = id(current)
                if identity in seen:
                    continue
                seen.add(identity)
                for key, child in reversed(list(current.items())):
                    key_category = self._field_category(key)
                    child_path = f"{path}.{self._safe_key(key)}"
                    if key_category and child is not None and child != "":
                        stack.append((child, child_path, depth + 1, key_category[0]))
                    else:
                        stack.append((child, child_path, depth + 1, None))
            elif isinstance(current, (list, tuple)):
                identity = id(current)
                if identity in seen:
                    continue
                seen.add(identity)
                for index in range(len(current) - 1, -1, -1):
                    stack.append((current[index], f"{path}[{index}]", depth + 1, None))
            elif current is None or isinstance(current, (bool, int, float)):
                continue
            else:
                # Unknown object types cannot be inspected safely as user data.
                limited = True
                break

        return {
            "status": "SCAN_LIMIT_EXCEEDED" if limited else ("SENSITIVE_DATA_FOUND" if findings else "CLEAR"),
            "findings": findings,
            "finding_count": len(findings),
            "checked_nodes": min(visited, self.max_nodes),
        }


class SensitiveDataGuard:
    """Explicit block-or-redact policy wrapper for data before provider transmission."""

    def __init__(self, *, mode: str = "block", scanner: SensitiveDataScanner | None = None) -> None:
        if mode not in {"block", "redact"}:
            raise ValueError("mode must be 'block' or 'redact'")
        if scanner is not None and not isinstance(scanner, SensitiveDataScanner):
            raise ValueError("scanner must be a SensitiveDataScanner")
        self.mode = mode
        self.scanner = scanner or SensitiveDataScanner()

    @staticmethod
    def _replacement(category: str) -> str:
        return f"[REDACTED:{category}]"

    def _redact_value(self, value: Any, *, depth: int, count: list[int], seen: set[int]) -> Any:
        count[0] += 1
        if count[0] > self.scanner.max_nodes or depth > self.scanner.max_depth:
            raise ValueError("redaction limit exceeded")
        if isinstance(value, str):
            if len(value) > self.scanner.max_string_length:
                raise ValueError("string limit exceeded")
            result = value
            for category, pattern, _ in self.scanner.PATTERNS:
                result = pattern.sub(self._replacement(category), result)
            return result
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen:
                raise ValueError("cyclic data cannot be redacted safely")
            seen.add(identity)
            result: dict[Any, Any] = {}
            for key, child in value.items():
                category = self.scanner._field_category(key)
                if category and child is not None and child != "":
                    result[key] = self._replacement(category[0])
                else:
                    result[key] = self._redact_value(child, depth=depth + 1, count=count, seen=seen)
            seen.remove(identity)
            return result
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                raise ValueError("cyclic data cannot be redacted safely")
            seen.add(identity)
            result = [self._redact_value(child, depth=depth + 1, count=count, seen=seen) for child in value]
            seen.remove(identity)
            return result
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise ValueError("unsupported data type")

    def inspect(self, value: Any) -> dict[str, Any]:
        result = self.scanner.scan(value)
        if result["status"] == "SCAN_LIMIT_EXCEEDED":
            return {"allowed": False, "status": "PRIVACY_SCAN_FAILED", "mode": self.mode, "scan": result}
        if result["finding_count"] == 0:
            return {"allowed": True, "status": "CLEAR", "mode": self.mode, "scan": result}
        if self.mode == "block":
            return {"allowed": False, "status": "SENSITIVE_DATA_BLOCKED", "mode": self.mode, "scan": result}
        return {"allowed": True, "status": "REDACTION_REQUIRED", "mode": self.mode, "scan": result}

    def sanitize(self, value: Any) -> dict[str, Any]:
        inspection = self.inspect(value)
        if not inspection["allowed"]:
            return {"allowed": False, "status": inspection["status"], "scan": inspection["scan"]}
        if inspection["status"] == "CLEAR":
            return {"allowed": True, "status": "CLEAR", "value": value, "scan": inspection["scan"]}
        try:
            sanitized = self._redact_value(value, depth=0, count=[0], seen=set())
        except Exception:
            return {"allowed": False, "status": "PRIVACY_REDACTION_FAILED", "scan": inspection["scan"]}
        return {"allowed": True, "status": "REDACTED", "value": sanitized, "scan": inspection["scan"]}
