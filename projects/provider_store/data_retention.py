"""Provider data-handling and local retention policy primitives.

This module evaluates caller-declared data categories/purpose/retention and can
purge expired local records. It cannot force an external provider to delete data;
provider policy facts must be sourced from authoritative documentation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


class ProviderDataRetentionPolicy:
    CATEGORIES = frozenset({
        "public", "personal", "confidential", "financial", "health", "biometric", "credentials",
    })

    def __init__(self, policies: dict[str, dict[str, Any]]) -> None:
        if not isinstance(policies, dict):
            raise ValueError("policies must be a mapping")
        normalized: dict[str, dict[str, Any]] = {}
        for provider_id, policy in policies.items():
            if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 128:
                raise ValueError("invalid provider ID")
            if not isinstance(policy, dict):
                raise ValueError("provider policy must be an object")
            categories = policy.get("allowed_data_categories")
            purposes = policy.get("allowed_purposes")
            max_days = policy.get("max_retention_days")
            training = policy.get("uses_data_for_training", True)
            if (
                not isinstance(categories, list) or not categories
                or any(not isinstance(item, str) or item not in self.CATEGORIES for item in categories)
                or "credentials" in categories
            ):
                raise ValueError("allowed_data_categories must be a non-empty list without credentials")
            if (
                not isinstance(purposes, list) or not purposes
                or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in purposes)
            ):
                raise ValueError("allowed_purposes must be a non-empty list of names")
            if (
                not isinstance(max_days, int) or isinstance(max_days, bool)
                or not 0 <= max_days <= 3650
            ):
                raise ValueError("max_retention_days must be an integer between 0 and 3650")
            if not isinstance(training, bool):
                raise ValueError("uses_data_for_training must be a boolean")
            normalized[provider_id] = {
                "allowed_data_categories": frozenset(categories),
                "allowed_purposes": frozenset(item.strip() for item in purposes),
                "max_retention_days": max_days,
                "uses_data_for_training": training,
            }
        self._policies = normalized

    def evaluate(
        self,
        provider_id: str,
        *,
        data_categories: list[str] | tuple[str, ...],
        purpose: str,
        retention_days: int,
        training_opt_in: bool = False,
    ) -> dict[str, Any]:
        """Fail closed when provider handling rules are absent or constraints mismatch."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"allowed": False, "status": "INVALID_PROVIDER"}
        policy = self._policies.get(provider_id)
        if policy is None:
            return {"allowed": False, "status": "DATA_HANDLING_POLICY_NOT_CONFIGURED", "provider": provider_id}
        if (
            not isinstance(data_categories, (list, tuple)) or not data_categories
            or any(not isinstance(item, str) or item not in self.CATEGORIES for item in data_categories)
        ):
            return {"allowed": False, "status": "INVALID_DATA_CATEGORIES", "provider": provider_id}
        if "credentials" in data_categories:
            return {"allowed": False, "status": "CREDENTIAL_DATA_FORBIDDEN", "provider": provider_id}
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 80:
            return {"allowed": False, "status": "INVALID_DATA_PURPOSE", "provider": provider_id}
        if (
            not isinstance(retention_days, int) or isinstance(retention_days, bool)
            or not 0 <= retention_days <= 3650
        ):
            return {"allowed": False, "status": "INVALID_RETENTION_PERIOD", "provider": provider_id}
        if not isinstance(training_opt_in, bool):
            return {"allowed": False, "status": "INVALID_TRAINING_CONSENT", "provider": provider_id}
        if not set(data_categories).issubset(policy["allowed_data_categories"]):
            return {"allowed": False, "status": "DATA_CATEGORY_NOT_ALLOWED", "provider": provider_id}
        if purpose.strip() not in policy["allowed_purposes"]:
            return {"allowed": False, "status": "DATA_PURPOSE_NOT_ALLOWED", "provider": provider_id}
        if retention_days > policy["max_retention_days"]:
            return {
                "allowed": False, "status": "RETENTION_PERIOD_EXCEEDED",
                "provider": provider_id, "max_retention_days": policy["max_retention_days"],
            }
        if policy["uses_data_for_training"] and not training_opt_in:
            return {"allowed": False, "status": "TRAINING_CONSENT_REQUIRED", "provider": provider_id}
        return {
            "allowed": True, "status": "DATA_POLICY_ALLOWED", "provider": provider_id,
            "max_retention_days": policy["max_retention_days"],
            "requested_retention_days": retention_days,
        }

    @staticmethod
    def retention_deadline(created_at: str | datetime, retention_days: int) -> str:
        if isinstance(created_at, str):
            try:
                timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("created_at must be an ISO-8601 timestamp") from None
        elif isinstance(created_at, datetime):
            timestamp = created_at
        else:
            raise ValueError("created_at must be an ISO-8601 timestamp")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if (
            not isinstance(retention_days, int) or isinstance(retention_days, bool)
            or not 0 <= retention_days <= 3650
        ):
            raise ValueError("retention_days must be an integer between 0 and 3650")
        return (timestamp.astimezone(timezone.utc) + timedelta(days=retention_days)).isoformat()

    @staticmethod
    def purge_expired_records(
        records: list[dict[str, Any]],
        *,
        now: datetime | None = None,
        timestamp_key: str = "created_at",
        retention_days: int,
    ) -> dict[str, Any]:
        """Return kept local records; expired or malformed-timestamp records are purged."""
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            return {"status": "INVALID_RECORDS", "kept": [], "purged": 0, "invalid_timestamp_purged": 0}
        if not isinstance(timestamp_key, str) or not timestamp_key.strip() or len(timestamp_key) > 80:
            return {"status": "INVALID_TIMESTAMP_KEY", "kept": [], "purged": 0, "invalid_timestamp_purged": 0}
        if (
            not isinstance(retention_days, int) or isinstance(retention_days, bool)
            or not 0 <= retention_days <= 3650
        ):
            return {"status": "INVALID_RETENTION_PERIOD", "kept": [], "purged": 0, "invalid_timestamp_purged": 0}
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            return {"status": "INVALID_CURRENT_TIME", "kept": [], "purged": 0, "invalid_timestamp_purged": 0}
        current = current.astimezone(timezone.utc)
        kept: list[dict[str, Any]] = []
        purged = 0
        invalid = 0
        for record in records:
            try:
                deadline = ProviderDataRetentionPolicy.retention_deadline(record.get(timestamp_key), retention_days)
                expires = datetime.fromisoformat(deadline)
            except (ValueError, TypeError):
                purged += 1
                invalid += 1
                continue
            if expires <= current:
                purged += 1
            else:
                kept.append(dict(record))
        return {
            "status": "PURGED" if purged else "NO_EXPIRED_RECORDS",
            "kept": kept,
            "purged": purged,
            "invalid_timestamp_purged": invalid,
        }
