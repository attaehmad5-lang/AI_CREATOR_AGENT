"""Bounded sensitive-data inspection for outbound provider payloads.

Findings contain categories and structural paths only; matched values are never returned.
This detector complements, but does not replace, consent, retention, access, or
provider data-handling controls.
"""
from __future__ import annotations

import re
from typing import Any


SENSITIVE_CATEGORIES = frozenset({
    "credential", "email", "phone", "financial", "government_id",
    "personal_identifier", "location", "health", "biometric",
    "network_identifier",
})

_TEXT_PATTERNS = (
    ("credential", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{12,}")),
    ("credential", re.compile(
        r"""(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|secret|password|passwd|credential|authorization)\b["']?\s*[:=]\s*["']?[^\s,;}"']{6,}"""
    )),
    ("credential", re.compile(
        r"(?i)\b(?:hf_[A-Za-z0-9]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})\b"
    )),
    ("credential", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("credential", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)),
    ("phone", re.compile(
        r"(?<![\w/-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w/-])"
    )),
    ("government_id", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
)

_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IBAN_PATTERN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b", re.IGNORECASE)

_FIELD_PATTERNS = (
    ("credential", re.compile(
        r"(?:^|_)(?:api_key|access_token|refresh_token|token|secret|client_secret|private_key|password|passwd|credential|authorization|auth_header|cookie|session_key)(?:_|$)"
    )),
    ("email", re.compile(r"(?:^|_)(?:email|email_address|contact_email)(?:_|$)")),
    ("phone", re.compile(r"(?:^|_)(?:phone|phone_number|telephone|mobile|fax)(?:_|$)")),
    ("financial", re.compile(
        r"(?:^|_)(?:credit_card|card_number|card_pan|cvv|cvc|iban|bank_account|account_number|routing_number|payment_account)(?:_|$)"
    )),
    ("government_id", re.compile(
        r"(?:^|_)(?:ssn|social_security_number|national_id|national_identifier|passport|passport_number|tax_id|drivers_license|driver_license|license_number)(?:_|$)"
    )),
    ("personal_identifier", re.compile(
        r"(?:^|_)(?:full_name|first_name|last_name|date_of_birth|birth_date|mother_maiden_name)(?:_|$)"
    )),
    ("location", re.compile(
        r"(?:^|_)(?:address|street_address|home_address|postal_address|gps|latitude|longitude|geolocation|precise_location|coordinates)(?:_|$)"
    )),
    ("health", re.compile(
        r"(?:^|_)(?:health|medical_record|health_record|diagnosis|medication|patient|lab_result|prescription|clinical_note)(?:_|$)"
    )),
    ("biometric", re.compile(
        r"(?:^|_)(?:biometric|face_embedding|face_template|fingerprint|voiceprint|retina_scan|iris_scan)(?:_|$)"
    )),
    ("network_identifier", re.compile(
        r"(?:^|_)(?:ip_address|client_ip|device_id|mac_address)(?:_|$)"
    )),
)
_SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def _normalized_field_name(key: Any) -> str:
    if not isinstance(key, str):
        return ""
    camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    return re.sub(r"[^A-Za-z0-9]+", "_", camel).strip("_").lower()


def _valid_luhn(candidate: str) -> bool:
    digits = [int(char) for char in candidate if char.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _valid_iban(candidate: str) -> bool:
    compact = re.sub(r"\s+", "", candidate).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return False
    rearranged = compact[4:] + compact[:4]
    remainder = 0
    for char in rearranged:
        expanded = str(ord(char) - 55) if char.isalpha() else char
        for digit in expanded:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


class SensitiveDataPolicy:
    """Inspect outbound payloads with bounded traversal and configurable blocking."""

    def __init__(
        self,
        *,
        blocked_categories: tuple[str, ...] = ("credential",),
        max_depth: int = 12,
        max_nodes: int = 500,
        max_text_chars: int = 20000,
        max_findings: int = 100,
    ) -> None:
        if not isinstance(blocked_categories, tuple) or any(
            not isinstance(value, str) or value not in SENSITIVE_CATEGORIES
            for value in blocked_categories
        ):
            raise ValueError("unsupported sensitive-data category")
        if len(set(blocked_categories)) != len(blocked_categories):
            raise ValueError("blocked categories must be unique")
        for value, name, upper in (
            (max_depth, "max_depth", 32),
            (max_nodes, "max_nodes", 10000),
            (max_text_chars, "max_text_chars", 1000000),
            (max_findings, "max_findings", 1000),
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
    def _field_category(key: Any) -> str | None:
        normalized = _normalized_field_name(key)
        for category, pattern in _FIELD_PATTERNS:
            if pattern.search(normalized):
                return category
        return None

    @staticmethod
    def _text_categories(value: str) -> set[str]:
        found = {category for category, pattern in _TEXT_PATTERNS if pattern.search(value)}
        if any(_valid_luhn(match.group(0)) for match in _CARD_PATTERN.finditer(value)):
            found.add("financial")
        if any(_valid_iban(match.group(0)) for match in _IBAN_PATTERN.finditer(value)):
            found.add("financial")
        return found

    def inspect(self, payload: Any) -> dict[str, Any]:
        findings: list[dict[str, str]] = []
        finding_keys: set[tuple[str, str]] = set()
        node_count = 0
        text_count = 0
        exceeded = False

        def add_finding(category: str, path: str) -> None:
            nonlocal exceeded
            identity = (category, path)
            if identity in finding_keys:
                return
            if len(findings) >= self.max_findings:
                exceeded = True
                return
            finding_keys.add(identity)
            findings.append({"category": category, "path": path})

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
                for category in self._text_categories(value):
                    add_finding(category, path)
                    if exceeded:
                        return
            elif isinstance(value, dict):
                for key, child in value.items():
                    key_text = key if isinstance(key, str) else ""
                    text_count += len(key_text)
                    if text_count > self.max_text_chars:
                        exceeded = True
                        return
                    child_path = self._child_path(path, key)
                    field_category = self._field_category(key)
                    if field_category is not None:
                        add_finding(field_category, child_path)
                        if exceeded:
                            return
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
                "blocked_categories": [],
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
