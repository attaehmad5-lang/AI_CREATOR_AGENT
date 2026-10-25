"""Fail-closed assessment of provider availability and degraded operating modes."""
from __future__ import annotations

import re
from typing import Any

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
_ALLOWED_STATES = {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN"}


class ProviderDegradedModeManager:
    """Assess availability only; does not reroute jobs or bypass provider policies."""

    def evaluate(
        self,
        *,
        required_categories: list[str],
        providers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(required_categories, list) or not required_categories:
            return {"status": "INVALID_REQUIRED_CATEGORIES", "mode": "UNKNOWN", "fail_closed": True}
        required = []
        for category in required_categories:
            if not isinstance(category, str) or not _ID.fullmatch(category):
                return {"status": "INVALID_REQUIRED_CATEGORIES", "mode": "UNKNOWN", "fail_closed": True}
            if category in required:
                return {"status": "DUPLICATE_REQUIRED_CATEGORY", "mode": "UNKNOWN", "fail_closed": True}
            required.append(category)
        if not isinstance(providers, list):
            return {"status": "INVALID_PROVIDER_SET", "mode": "UNKNOWN", "fail_closed": True}

        available: dict[str, list[str]] = {category: [] for category in required}
        degraded: dict[str, list[str]] = {category: [] for category in required}
        seen: set[str] = set()
        for item in providers:
            if not isinstance(item, dict):
                return {"status": "INVALID_PROVIDER_RECORD", "mode": "UNKNOWN", "fail_closed": True}
            provider_id = item.get("provider_id")
            state = item.get("state")
            categories = item.get("categories")
            if (
                not isinstance(provider_id, str) or not _ID.fullmatch(provider_id)
                or provider_id in seen or state not in _ALLOWED_STATES
                or not isinstance(categories, list)
                or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in categories)
            ):
                return {"status": "INVALID_PROVIDER_RECORD", "mode": "UNKNOWN", "fail_closed": True}
            seen.add(provider_id)
            if len(set(categories)) != len(categories):
                return {"status": "INVALID_PROVIDER_RECORD", "mode": "UNKNOWN", "fail_closed": True}
            for category in required:
                if category in categories and state == "HEALTHY":
                    available[category].append(provider_id)
                elif category in categories and state == "DEGRADED":
                    degraded[category].append(provider_id)

        missing = [category for category in required if not available[category] and not degraded[category]]
        degraded_only = [category for category in required if not available[category] and degraded[category]]
        if not missing and not degraded_only and all(available.values()):
            mode, status = "NORMAL", "ALL_REQUIRED_CAPABILITIES_HEALTHY"
        elif not all(not available[c] and not degraded[c] for c in required):
            mode, status = "DEGRADED", "PARTIAL_CAPABILITY_AVAILABILITY"
        else:
            mode, status = "OUTAGE", "NO_REQUIRED_CAPABILITIES_AVAILABLE"

        return {
            "status": status,
            "mode": mode,
            "fail_closed": mode == "OUTAGE",
            "required_categories": required,
            "healthy_providers_by_category": available,
            "degraded_providers_by_category": degraded,
            "missing_categories": missing,
            "degraded_only_categories": degraded_only,
            "recommendation": (
                "Continue normal routing subject to existing policy and health gates."
                if mode == "NORMAL" else
                "Use only healthy eligible providers; enforce existing cost, privacy, region, tenant, and approval policies."
                if mode == "DEGRADED" else
                "Reject new work for unavailable capabilities, preserve queued jobs, and expose recovery diagnostics."
            ),
            "execution_started": False,
        }
