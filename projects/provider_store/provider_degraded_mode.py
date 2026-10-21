"""Fail-closed degraded-mode assessment for partial provider outages."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_STATES = {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN"}
_MODES = {"NORMAL", "DEGRADED", "OUTAGE"}


class ProviderDegradedModeManager:
    """Assess whether provider capacity can safely accept new work.

    This is a deterministic policy primitive. Callers must supply fresh health
    evidence; the manager does not invent probes or treat missing providers as healthy.
    """

    def __init__(self, *, minimum_healthy_providers: int = 1) -> None:
        if (
            isinstance(minimum_healthy_providers, bool)
            or not isinstance(minimum_healthy_providers, int)
            or not 1 <= minimum_healthy_providers <= 10000
        ):
            raise ValueError("minimum_healthy_providers is outside safe limits")
        self.minimum_healthy_providers = minimum_healthy_providers

    def evaluate(
        self,
        provider_states: Any,
        *,
        max_age_seconds: float | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_states, dict) or not provider_states:
            return {
                "status": "INVALID_PROVIDER_STATES",
                "mode": "OUTAGE",
                "accepting_new_work": False,
                "healthy_providers": [],
                "unavailable_providers": [],
                "reason_codes": ["NO_VALID_HEALTH_EVIDENCE"],
            }
        age_limit = None
        if max_age_seconds is not None:
            try:
                age_limit = float(max_age_seconds)
            except (TypeError, ValueError, OverflowError):
                age_limit = None
            if (
                isinstance(max_age_seconds, bool)
                or age_limit is None
                or not math.isfinite(age_limit)
                or age_limit < 0
            ):
                return {
                    "status": "INVALID_HEALTH_POLICY",
                    "mode": "OUTAGE",
                    "accepting_new_work": False,
                    "healthy_providers": [],
                    "unavailable_providers": [],
                    "reason_codes": ["INVALID_MAX_HEALTH_AGE"],
                }
        current_time = now or datetime.now(timezone.utc)
        if not isinstance(current_time, datetime):
            return {
                "status": "INVALID_HEALTH_POLICY",
                "mode": "OUTAGE",
                "accepting_new_work": False,
                "healthy_providers": [],
                "unavailable_providers": [],
                "reason_codes": ["INVALID_CURRENT_TIME"],
            }
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        normalized_states: dict[str, str] = {}
        for provider_id, evidence in provider_states.items():
            if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
                return {
                    "status": "INVALID_PROVIDER_STATES",
                    "mode": "OUTAGE",
                    "accepting_new_work": False,
                    "healthy_providers": [],
                    "unavailable_providers": [],
                    "reason_codes": ["INVALID_HEALTH_EVIDENCE"],
                }
            if age_limit is None:
                state = evidence
            else:
                if not isinstance(evidence, dict) or not {"status", "checked_at"}.issubset(evidence):
                    return {
                        "status": "INVALID_PROVIDER_STATES",
                        "mode": "OUTAGE",
                        "accepting_new_work": False,
                        "healthy_providers": [],
                        "unavailable_providers": [],
                        "reason_codes": ["TIMESTAMPED_HEALTH_EVIDENCE_REQUIRED"],
                    }
                state = evidence.get("status")
                checked_at = evidence.get("checked_at")
                if not isinstance(checked_at, str):
                    return {
                        "status": "INVALID_PROVIDER_STATES",
                        "mode": "OUTAGE",
                        "accepting_new_work": False,
                        "healthy_providers": [],
                        "unavailable_providers": [],
                        "reason_codes": ["INVALID_HEALTH_TIMESTAMP"],
                    }
                try:
                    checked = datetime.fromisoformat(checked_at)
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                    age = (current_time.astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    return {
                        "status": "INVALID_PROVIDER_STATES",
                        "mode": "OUTAGE",
                        "accepting_new_work": False,
                        "healthy_providers": [],
                        "unavailable_providers": [],
                        "reason_codes": ["INVALID_HEALTH_TIMESTAMP"],
                    }
                if age < 0:
                    state = "UNKNOWN"
                elif age > age_limit:
                    state = "UNKNOWN"
            if not isinstance(state, str) or state not in _STATES:
                return {
                    "status": "INVALID_PROVIDER_STATES",
                    "mode": "OUTAGE",
                    "accepting_new_work": False,
                    "healthy_providers": [],
                    "unavailable_providers": [],
                    "reason_codes": ["INVALID_HEALTH_EVIDENCE"],
                }
            normalized_states[provider_id] = state

        healthy = sorted(
            provider_id for provider_id, state in normalized_states.items()
            if state == "HEALTHY"
        )
        unavailable = sorted(
            provider_id for provider_id, state in normalized_states.items()
            if state != "HEALTHY"
        )
        if not healthy:
            mode = "OUTAGE"
            reasons = ["NO_HEALTHY_PROVIDERS"]
        elif len(healthy) < self.minimum_healthy_providers:
            mode = "OUTAGE"
            reasons = ["HEALTHY_CAPACITY_BELOW_MINIMUM"]
        elif unavailable:
            mode = "DEGRADED"
            reasons = ["PARTIAL_PROVIDER_UNAVAILABILITY"]
        else:
            mode = "NORMAL"
            reasons = []

        return {
            "status": "DEGRADED_MODE_ASSESSED",
            "mode": mode,
            "accepting_new_work": mode != "OUTAGE",
            "healthy_providers": healthy,
            "unavailable_providers": unavailable,
            "reason_codes": reasons,
        }
