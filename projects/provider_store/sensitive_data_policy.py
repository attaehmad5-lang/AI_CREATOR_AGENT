"""Bounded sensitive-data inspection for outbound provider payloads.

Findings report categories and structural paths only; matched values are never returned.
"""
from __future__ import annotations

import re
from typing import Any

_PATTERNS = (
    ("credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}")),
    ("credential", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]{6,}")),
    ("credential", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"(?<![\w/-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w/-])")),
)
_SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def _sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return (
        normalized in {"apikey", "auth", "authheader", "privatekey"}
        or normalized.endswith(("token", "secret", "password", "passwd", "credential", "credentials"))
        or "authorization" in normalized
    )


class SensitiveDataPolicy:
    def __init__(
        self,
        *,
        blocked_categories: tuple[str, ...] = ("credential",),
        max_depth: int = 12,
        max_nodes: int = 500,
        max_text_chars: int = 20000,
    ) -> None:
        if not isinstance(blocked_categories, tuple) or any(
            value not in {"credential", "email", "phone"} for value in blocked_categories
        ):
            raise ValueError("unsupported sensitive-data category")
        if len(set(blocked_categories)) != len(blocked_categories):
            raise ValueError("blocked categories must be unique")
        for value, name, upper in (
            (max_depth, "max_depth", 32),
            (max_nodes, "max_nodes", 10000),
            (max_text_chars, "max_text_chars", 1000000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{name} is outside safe limits")
        self.blocked_categories = frozenset(blocked_categories)
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_text_chars = max_text_chars

    @staticmethod
    def _child_path(parent: str, key: Any) -> str:
        if isinstance(key, int):
            return f"{parent}[{key}]"
        if isinstance(key, str) and _SAFE_KEY.fullmatch(key):
            return f"{parent}.{key}"
        return f"{parent}.[key]"

    def inspect(self, payload: Any) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        node_count = 0
        text_count = 0
        exceeded = False

        def walk(value: Any, path: str, depth: int) -> None:
            nonlocal node_count, text_count, exceeded
            if exceeded:
                return
            node_count += 1
            if node_count > self.max_nodes or depth > self.max_depth:
                exceeded = True
                return
            if isinstance(value, str):
                text_count += len(value)
                if text_count > self.max_text_chars:
                    exceeded = True
                    return
                for category, pattern in _PATTERNS:
                    if pattern.search(value):
                        item = {"category": category, "path": path}
                        if item not in findings:
                            findings.append(item)
                        if len(findings) >= 100:
                            exceeded = True
                            return
            elif isinstance(value, dict):
                for key, child in value.items():
                    child_path = self._child_path(path, key)
                    if _sensitive_key(key):
                        item = {"category": "credential", "path": child_path}
                        if item not in findings:
                            findings.append(item)
                        if "credential" in self.blocked_categories:
                            # Continue bounded traversal for a complete category report,
                            # but never expose the field's value.
                            pass
                    walk(child, child_path, depth + 1)
                    if exceeded:
                        return
            elif isinstance(value, (list, tuple)):
                for index, child in enumerate(value):
                    walk(child, self._child_path(path, index), depth + 1)
                    if exceeded:
                        return

        walk(payload, "$", 0)
        if exceeded:
            return {
                "allowed": False,
                "status": "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED",
                "findings": [],
            }
        blocked = [item for item in findings if item["category"] in self.blocked_categories]
        return {
            "allowed": not bool(blocked),
            "status": "SENSITIVE_DATA_BLOCKED" if blocked else (
                "SENSITIVE_DATA_DETECTED" if findings else "SENSITIVE_DATA_CLEAR"
            ),
            "findings": findings,
            "blocked_categories": sorted({item["category"] for item in blocked}),
        }
