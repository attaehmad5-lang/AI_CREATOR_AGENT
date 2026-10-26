"""Cooldown-aware provider health recheck coordinator.

This component schedules explicit probes; it never starts generation or changes routing.
A caller supplies provider-specific probes. Health state remains owned by ProviderHealthManager.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
import re

_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")


class ProviderRecoveryCoordinator:
    """Run due probes for unhealthy/degraded providers with fail-closed validation."""

    def __init__(
        self,
        health_manager: Any,
        probes: dict[str, Callable[..., Any]],
        *,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(health_manager, "active_probe", None)):
            raise ValueError("health_manager must provide active_probe")
        if not isinstance(probes, dict):
            raise ValueError("probes must be a provider-to-callable mapping")
        if isinstance(cooldown_seconds, bool) or not isinstance(cooldown_seconds, (int, float)):
            raise ValueError("cooldown_seconds must be a non-negative finite number")
        if cooldown_seconds < 0 or cooldown_seconds == float("inf") or cooldown_seconds != cooldown_seconds:
            raise ValueError("cooldown_seconds must be a non-negative finite number")
        normalized: dict[str, Callable[..., Any]] = {}
        for provider_id, probe in probes.items():
            if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
                raise ValueError("invalid provider ID in probes")
            if not callable(probe):
                raise ValueError("each provider probe must be callable")
            normalized[provider_id] = probe
        self.health_manager = health_manager
        self.probes = normalized
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not callable(self.clock):
            raise ValueError("clock must be callable")

    def _snapshot(self) -> dict[str, Any]:
        path = getattr(self.health_manager, "state_path", None)
        if not isinstance(path, (str, Path)):
            raise ValueError("health manager state_path is unavailable")
        try:
            with Path(path).open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider health snapshot is unavailable or invalid") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("providers"), dict):
            raise ValueError("provider health snapshot has invalid shape")
        return payload["providers"]

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid provider failure timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid provider failure timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def run_due_rechecks(self) -> dict[str, Any]:
        """Probe only due unhealthy/degraded providers; malformed state blocks all probes."""
        try:
            providers = self._snapshot()
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("clock must return timezone-aware datetime")
            now = now.astimezone(timezone.utc)
            due: list[str] = []
            skipped: dict[str, str] = {}
            for provider_id, probe in self.probes.items():
                record = providers.get(provider_id)
                if not isinstance(record, dict):
                    skipped[provider_id] = "NO_HEALTH_RECORD"
                    continue
                status = record.get("status")
                if status not in {"UNHEALTHY", "DEGRADED"}:
                    skipped[provider_id] = "NOT_RECOVERY_CANDIDATE"
                    continue
                failure_at = record.get("last_failure_at")
                if not failure_at:
                    skipped[provider_id] = "NO_FAILURE_TIMESTAMP"
                    continue
                failed = self._timestamp(failure_at)
                age = (now - failed).total_seconds()
                if age < 0:
                    skipped[provider_id] = "FUTURE_FAILURE_TIMESTAMP"
                    continue
                if age < self.cooldown_seconds:
                    skipped[provider_id] = "COOLDOWN_ACTIVE"
                    continue
                due.append(provider_id)
        except (ValueError, TypeError, OverflowError):
            return {"status": "BLOCKED_INVALID_HEALTH_STATE", "probed": [], "skipped": {}, "results": {}}

        results: dict[str, Any] = {}
        for provider_id in due:
            try:
                result = self.health_manager.active_probe(provider_id, self.probes[provider_id])
            except Exception as exc:
                results[provider_id] = {
                    "status": "PROBE_COORDINATION_ERROR",
                    "verified": False,
                    "error_type": type(exc).__name__,
                }
                continue
            if not isinstance(result, dict) or result.get("provider") != provider_id:
                results[provider_id] = {"status": "INVALID_PROBE_RESULT", "verified": False}
                continue
            result_status = result.get("status")
            result_verified = result.get("verified")
            # Probe status is surfaced to callers and may be persisted by them.
            # Accept only a bounded machine-readable token and a real bool; do not
            # pass through arbitrary objects, multiline text, or truthy strings.
            if (
                not isinstance(result_status, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", result_status)
                or not isinstance(result_verified, bool)
            ):
                results[provider_id] = {"status": "INVALID_PROBE_RESULT", "verified": False}
                continue
            results[provider_id] = {
                "status": result_status,
                "verified": result_verified,
            }
        return {
            "status": "COMPLETED",
            "probed": due,
            "skipped": skipped,
            "results": results,
        }
