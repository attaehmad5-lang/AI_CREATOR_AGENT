"""Fail-closed provider data-retention and training-use policy checks."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

_ALLOWED_CATEGORIES = frozenset({
    "public", "user_content", "personal", "credential", "financial",
    "health", "biometric", "confidential",
})
_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ProviderDataHandlingPolicy:
    """Evaluate explicit provider data-handling rules; never infer missing terms."""

    def __init__(self, policies: dict[str, dict[str, Any]]) -> None:
        if not isinstance(policies, dict):
            raise ValueError("provider data policies must be a mapping")
        self.policies: dict[str, dict[str, Any]] = {}
        for provider_id, policy in policies.items():
            if not isinstance(provider_id, str) or not _POLICY_ID.fullmatch(provider_id):
                raise ValueError("invalid provider ID in data policy")
            if not isinstance(policy, dict):
                raise ValueError("invalid provider data policy")
            categories = policy.get("allowed_categories")
            retention = policy.get("max_retention_days")
            training = policy.get("training_use_allowed")
            source = policy.get("source_url")
            if (
                not isinstance(categories, (list, tuple))
                or any(not isinstance(item, str) or item not in _ALLOWED_CATEGORIES for item in categories)
                or len(set(categories)) != len(categories)
                or isinstance(retention, bool) or not isinstance(retention, int)
                or not 0 <= retention <= 3650
                or not isinstance(training, bool)
                or not isinstance(source, str)
            ):
                raise ValueError("invalid provider data policy fields")
            if (
                len(source) > 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in source)
            ):
                raise ValueError("provider data policy source URL contains unsafe characters")
            parsed = urlsplit(source)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("provider data policy source URL has an invalid port") from exc
            if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or (port is not None and not 1 <= port <= 65535)
            ):
                raise ValueError("provider data policy source must be a clean HTTPS terms URL")
            self.policies[provider_id] = {
                "allowed_categories": frozenset(categories),
                "max_retention_days": retention,
                "training_use_allowed": training,
                "source_url": source,
            }

    def evaluate(
        self,
        *,
        provider_id: str,
        data_categories: tuple[str, ...] | list[str],
        retention_days: int = 0,
        provider_training_use: bool = False,
        user_training_consent: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"allowed": False, "status": "INVALID_PROVIDER_ID"}
        if not isinstance(data_categories, (tuple, list)) or any(
            not isinstance(item, str) or item not in _ALLOWED_CATEGORIES for item in data_categories
        ):
            return {"allowed": False, "status": "INVALID_DATA_CATEGORIES"}
        if len(set(data_categories)) != len(data_categories):
            return {"allowed": False, "status": "DUPLICATE_DATA_CATEGORIES"}
        if isinstance(retention_days, bool) or not isinstance(retention_days, int) or not 0 <= retention_days <= 3650:
            return {"allowed": False, "status": "INVALID_RETENTION_PERIOD"}
        if not isinstance(provider_training_use, bool) or not isinstance(user_training_consent, bool):
            return {"allowed": False, "status": "INVALID_TRAINING_CONSENT"}
        policy = self.policies.get(provider_id)
        if policy is None:
            return {"allowed": False, "status": "PROVIDER_DATA_POLICY_UNKNOWN", "provider": provider_id}
        categories = set(data_categories)
        if "credential" in categories:
            return {"allowed": False, "status": "CREDENTIAL_DATA_NEVER_ALLOWED", "provider": provider_id}
        disallowed = sorted(categories - policy["allowed_categories"])
        if disallowed:
            return {
                "allowed": False, "status": "DATA_CATEGORY_NOT_ALLOWED",
                "provider": provider_id, "disallowed_categories": disallowed,
                "policy_source": policy["source_url"],
            }
        if retention_days > policy["max_retention_days"]:
            return {
                "allowed": False, "status": "RETENTION_PERIOD_EXCEEDS_PROVIDER_POLICY",
                "provider": provider_id,
                "max_retention_days": policy["max_retention_days"],
                "policy_source": policy["source_url"],
            }
        if provider_training_use and (
            policy["training_use_allowed"] is not True or user_training_consent is not True
        ):
            return {
                "allowed": False, "status": "PROVIDER_TRAINING_USE_NOT_AUTHORIZED",
                "provider": provider_id,
                "policy_source": policy["source_url"],
            }
        return {
            "allowed": True, "status": "DATA_POLICY_ALLOWED",
            "provider": provider_id,
            "retention_days": retention_days,
            "provider_training_use": provider_training_use,
            "policy_source": policy["source_url"],
        }
