"""Bounded sensitive-data inspection for provider-bound payloads.

Only safe categories and structural paths are returned; matched values and
arbitrary object representations are never exposed.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_PATTERNS = (
    ("credential", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("credential", re.compile(r'''(?i)\b(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret(?:[_-]?key)?|password|passwd|credential|authorization|proxy[_-]?authorization|cookie|session(?:[_-]?token)?|private[_-]?key|signing[_-]?key)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)''')),
    ("credential", re.compile(r"(?i)\b(?:sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{8,}|AIza[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("credential", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("credential", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL)),
    ("credential", re.compile(r"(?i)\bhttps?://[^/@:\s]+:[^/@\s]+@")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"(?<![\w/-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w/-])")),
)
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SENSITIVE_FIELD = re.compile(r"(?i)(?:^|[_-])(?:api[_-]?(?:key|token)|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret(?:[_-]?key)?|password|passwd|credential|authorization|proxy[_-]?authorization|cookie|session(?:[_-]?token)?|private[_-]?key|signing[_-]?key)(?:$|[_-])")
_REFERENCE_SUFFIXES = ("_env", "_ref", "_reference", "_configured", "_present", "_name")


def _has_material_value(value: Any) -> bool:
    return value is not None and value != "" and value is not False


def _is_luhn_valid(candidate: str) -> bool:
    digits = "".join(char for char in candidate if char.isdigit())
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


class SensitiveDataPolicy:
    """Inspect nested outbound values with explicit limits and configurable blocking."""

    CATEGORIES = frozenset({"credential", "email", "phone", "payment_card"})

    def __init__(
        self,
        *,
        blocked_categories: tuple[str, ...] = ("credential",),
        max_depth: int = 12,
        max_nodes: int = 500,
        max_text_chars: int = 20000,
        max_findings: int = 100,
    ) -> None:
        if not isinstance(blocked_categories, tuple) or any(value not in self.CATEGORIES for value in blocked_categories):
            raise ValueError("unsupported sensitive-data category")
        if len(set(blocked_categories)) != len(blocked_categories):
            raise ValueError("blocked categories must be unique")
        for value, name, upper in (
            (max_depth, "max_depth", 32),
            (max_nodes, "max_nodes", 10000),
            (max_text_chars, "max_text_chars", 1000000),
            (max_findings, "max_findings", 500),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise ValueError(f"{name} is outside safe limits")
        self.blocked_categories = frozenset(blocked_categories)
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_text_chars = max_text_chars
        self.max_findings = max_findings

    @staticmethod
    def _child_path(parent: str, key: Any) -> str:
        if isinstance(key, int) and not isinstance(key, bool):
            return f"{parent}[{key}]"
        if isinstance(key, str) and _SAFE_KEY.fullmatch(key):
            return f"{parent}.{key}"
        return f"{parent}.[key]"

    @staticmethod
    def _sensitive_field_name(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key.strip()).lower().replace(" ", "_")
        if normalized.endswith(_REFERENCE_SUFFIXES):
            return False
        return bool(_SENSITIVE_FIELD.search(normalized))

    def inspect(self, payload: Any) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        seen_findings: set[tuple[str, str]] = set()
        active_containers: set[int] = set()
        node_count = 0
        text_count = 0
        exceeded = False

        def add_finding(category: str, path: str) -> None:
            nonlocal exceeded
            item = (category, path)
            if item in seen_findings:
                return
            if len(findings) >= self.max_findings:
                exceeded = True
                return
            seen_findings.add(item)
            findings.append({"category": category, "path": path})

        def scan_text(value: str, path: str) -> None:
            nonlocal text_count, exceeded
            text_count += len(value)
            if text_count > self.max_text_chars:
                exceeded = True
                return
            for category, pattern in _PATTERNS:
                if pattern.search(value):
                    add_finding(category, path)
                    if exceeded:
                        return
            if any(_is_luhn_valid(match.group(0)) for match in _CARD_CANDIDATE.finditer(value)):
                add_finding("payment_card", path)

        def walk(value: Any, path: str, depth: int) -> None:
            nonlocal node_count, exceeded
            if exceeded:
                return
            node_count += 1
            if node_count > self.max_nodes or depth > self.max_depth:
                exceeded = True
                return
            if isinstance(value, str):
                scan_text(value, path)
                return
            if isinstance(value, Mapping):
                identity = id(value)
                if identity in active_containers:
                    exceeded = True
                    return
                active_containers.add(identity)
                try:
                    for key, child in value.items():
                        child_path = self._child_path(path, key)
                        if self._sensitive_field_name(key) and _has_material_value(child):
                            add_finding("credential", child_path)
                        walk(child, child_path, depth + 1)
                        if exceeded:
                            return
                finally:
                    active_containers.remove(identity)
                return
            if isinstance(value, (list, tuple)):
                identity = id(value)
                if identity in active_containers:
                    exceeded = True
                    return
                active_containers.add(identity)
                try:
                    for index, child in enumerate(value):
                        walk(child, self._child_path(path, index), depth + 1)
                        if exceeded:
                            return
                finally:
                    active_containers.remove(identity)

        try:
            walk(payload, "$", 0)
        except Exception:
            return {"allowed": False, "status": "SENSITIVE_DATA_SCAN_FAILED", "findings": [], "blocked_categories": []}

        if exceeded:
            return {"allowed": False, "status": "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED", "findings": [], "blocked_categories": []}
        blocked = [item for item in findings if item["category"] in self.blocked_categories]
        return {
            "allowed": not bool(blocked),
            "status": "SENSITIVE_DATA_BLOCKED" if blocked else ("SENSITIVE_DATA_DETECTED" if findings else "SENSITIVE_DATA_CLEAR"),
            "findings": findings,
            "blocked_categories": sorted({item["category"] for item in blocked}),
        }
