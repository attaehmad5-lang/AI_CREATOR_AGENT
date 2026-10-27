"""Event-driven outage detection and incident recovery using verified health evidence."""
from __future__ import annotations

import re
from typing import Any

from .health_manager import ProviderHealthManager
from .provider_incident_manager import ProviderIncidentManager

_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")


class ProviderOutageDetector:
    """Translate stale/unhealthy health evidence into deduplicated incidents.

    This detector is deliberately event-driven: callers invoke evaluate() after a
    probe or on their own schedule. It does not create background threads or claim
    a live outage state when no valid health evidence exists.
    """

    def __init__(
        self,
        health_manager: ProviderHealthManager,
        incident_manager: ProviderIncidentManager,
        *,
        stale_after_seconds: float = 300.0,
    ) -> None:
        if not isinstance(health_manager, ProviderHealthManager):
            raise ValueError("health_manager must be a ProviderHealthManager")
        if not isinstance(incident_manager, ProviderIncidentManager):
            raise ValueError("incident_manager must be a ProviderIncidentManager")
        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, (int, float))
            or stale_after_seconds < 0
        ):
            raise ValueError("stale_after_seconds must be a non-negative number")
        self.health_manager = health_manager
        self.incident_manager = incident_manager
        self.stale_after_seconds = float(stale_after_seconds)

    def evaluate(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "outage": False}
        health = self.health_manager.is_eligible(
            provider_id, stale_after_seconds=self.stale_after_seconds
        )
        health_status = health.get("status")
        if health_status in {"UNHEALTHY", "STALE"}:
            code = (
                "PROVIDER_UNHEALTHY"
                if health_status == "UNHEALTHY"
                else "PROVIDER_HEALTH_STALE"
            )
            incident = self.incident_manager.record(
                provider_id=provider_id,
                category="provider_health",
                severity="error" if health_status == "UNHEALTHY" else "warning",
                code=code,
            )
            if not incident.get("saved"):
                return {"status": "INCIDENT_WRITE_FAILED", "outage": True}
            return {
                "status": "OUTAGE_DETECTED",
                "outage": True,
                "health_status": health_status,
                "incident_id": incident.get("incident", {}).get("incident_id"),
                "incident_status": incident.get("status"),
            }
        if health.get("eligible") is True and health_status == "HEALTHY":
            resolved = 0
            snapshot = self.incident_manager.snapshot()
            for incident in snapshot.get("incidents", []):
                if (
                    incident.get("provider_id") == provider_id
                    and incident.get("category") == "provider_health"
                    and incident.get("code") in {
                        "PROVIDER_UNHEALTHY", "PROVIDER_HEALTH_STALE"
                    }
                    and incident.get("state") != "RESOLVED"
                ):
                    result = self.incident_manager.resolve(incident["incident_id"])
                    if result.get("saved") and result.get("status") == "INCIDENT_RESOLVED":
                        resolved += 1
            return {
                "status": "OUTAGE_CLEARED" if resolved else "PROVIDER_HEALTHY",
                "outage": False,
                "resolved_incidents": resolved,
            }
        return {
            "status": "OUTAGE_STATE_UNKNOWN",
            "outage": None,
            "health_status": health_status,
            "reason": health.get("reason", "HEALTH_EVIDENCE_INSUFFICIENT"),
        }
