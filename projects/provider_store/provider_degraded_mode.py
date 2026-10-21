"""Fail-closed degraded-mode assessment for partial provider outages."""
from __future__ import annotations

import re
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

    def evaluate(self, provider_states: Any) -> dict[str, Any]:
        if not isinstance(provider_states, dict) or not provider_states:
            return {
                "status": "INVALID_PROVIDER_STATES",
                "mode": "OUTAGE",
                "accepting_new_work": False,
                "healthy_providers": [],
                "unavailable_providers": [],
                "reason_codes": ["NO_VALID_HEALTH_EVIDENCE"],
            }
        for provider_id, state in provider_states.items():
            if (
                not isinstance(provider_id, str)
                or not _PROVIDER_ID_RE.fullmatch(provider_id)
                or not isinstance(state, str)
                or state not in _STATES
            ):
                return {
                    "status": "INVALID_PROVIDER_STATES",
                    "mode": "OUTAGE",
                    "accepting_new_work": False,
                    "healthy_providers": [],
                    "unavailable_providers": [],
                    "reason_codes": ["INVALID_HEALTH_EVIDENCE"],
                }

        healthy = sorted(
            provider_id for provider_id, state in provider_states.items()
            if state == "HEALTHY"
        )
        unavailable = sorted(
            provider_id for provider_id, state in provider_states.items()
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
