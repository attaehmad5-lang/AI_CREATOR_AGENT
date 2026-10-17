"""Persistent, bounded service-level objective definitions and evaluation."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


class ProviderSLOManager:
    """Track request-level success/latency evidence without storing payloads."""

    MAX_WINDOW_SECONDS = 30 * 24 * 60 * 60

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_slos: int = 500,
        max_observations_per_slo: int = 100_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_slos, bool) or not isinstance(max_slos, int) or not 1 <= max_slos <= 10_000:
            raise ValueError("max_slos must be between 1 and 10000")
        if isinstance(max_observations_per_slo, bool) or not isinstance(max_observations_per_slo, int) or not 1 <= max_observations_per_slo <= 1_000_000:
            raise ValueError("max_observations_per_slo must be between 1 and 1000000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(state_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_slos = max_slos
        self.max_observations = max_observations_per_slo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "slos": {}}
        with self._transaction():
            pass

    @staticmethod
    def _valid_id(value: Any, limit: int = 160) -> bool:
        return (
            isinstance(value, str) and 1 <= len(value.strip()) <= limit
            and value == value.strip() and not any(ord(ch) < 32 for ch in value)
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid SLO timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid SLO timestamp") from None
        if parsed.tzinfo is None:
            raise ValueError("SLO timestamp must include timezone")
        return parsed.astimezone(timezone.utc)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("SLO clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": "1.0", "slos": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("SLO state is unreadable") from None
        if not isinstance(raw, dict) or raw.get("version") != "1.0" or not isinstance(raw.get("slos"), dict):
            raise ValueError("SLO state schema is invalid")
        if len(raw["slos"]) > self.max_slos:
            raise ValueError("SLO state exceeds configured limit")
        for slo_id, slo in raw["slos"].items():
            if not self._valid_id(slo_id, 128) or not isinstance(slo, dict) or slo.get("slo_id") != slo_id:
                raise ValueError("SLO definition is invalid")
            if not self._valid_id(slo.get("service_id"), 128):
                raise ValueError("SLO service is invalid")
            target = finite_number(slo.get("target_success_rate"), minimum=0, maximum=1)
            window = slo.get("window_seconds")
            minimum = slo.get("min_samples")
            if (
                target is None or target <= 0
                or isinstance(window, bool) or not isinstance(window, int)
                or not 60 <= window <= self.MAX_WINDOW_SECONDS
                or isinstance(minimum, bool) or not isinstance(minimum, int)
                or not 1 <= minimum <= self.max_observations
                or slo.get("status") not in {"ACTIVE", "PAUSED"}
            ):
                raise ValueError("SLO policy is invalid")
            latency = slo.get("max_p95_latency_ms")
            if latency is not None and finite_number(latency, minimum=0) is None:
                raise ValueError("SLO latency threshold is invalid")
            observations = slo.get("observations")
            if not isinstance(observations, list) or len(observations) > self.max_observations:
                raise ValueError("SLO observations are invalid")
            seen: set[str] = set()
            for observation in observations:
                if not isinstance(observation, dict):
                    raise ValueError("SLO observation is invalid")
                request_id = observation.get("request_id")
                if not self._valid_id(request_id, 200) or request_id in seen:
                    raise ValueError("SLO observation id is invalid or duplicated")
                seen.add(request_id)
                self._timestamp(observation.get("observed_at"))
                if not isinstance(observation.get("success"), bool):
                    raise ValueError("SLO observation success flag is invalid")
                if finite_number(observation.get("latency_ms"), minimum=0) is None:
                    raise ValueError("SLO observation latency is invalid")
        return raw

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    class _Transaction:
        def __init__(self, manager: "ProviderSLOManager") -> None:
            self.manager = manager
        def __enter__(self):
            self.manager._lock.acquire()
            self.lock = interprocess_file_lock(self.manager.lock_path)
            self.lock.__enter__()
            try:
                self.manager.data = self.manager._load()
            except Exception:
                self.lock.__exit__(None, None, None)
                self.manager._lock.release()
                raise
            return self.manager
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.manager._save()
            finally:
                self.lock.__exit__(exc_type, exc, tb)
                self.manager._lock.release()
            return False

    def _transaction(self):
        return self._Transaction(self)

    def define_slo(
        self,
        *,
        slo_id: str,
        service_id: str,
        target_success_rate: float,
        window_seconds: int = 86_400,
        min_samples: int = 20,
        max_p95_latency_ms: float | None = None,
    ) -> dict[str, Any]:
        target = finite_number(target_success_rate, minimum=0, maximum=1)
        latency = None if max_p95_latency_ms is None else finite_number(max_p95_latency_ms, minimum=0)
        if (
            not self._valid_id(slo_id, 128) or not self._valid_id(service_id, 128)
            or target is None or target <= 0
            or isinstance(window_seconds, bool) or not isinstance(window_seconds, int)
            or not 60 <= window_seconds <= self.MAX_WINDOW_SECONDS
            or isinstance(min_samples, bool) or not isinstance(min_samples, int)
            or not 1 <= min_samples <= self.max_observations
            or (max_p95_latency_ms is not None and latency is None)
        ):
            return {"status": "INVALID_SLO_DEFINITION", "saved": False}
        with self._transaction():
            if slo_id in self.data["slos"]:
                return {"status": "SLO_ALREADY_EXISTS", "saved": False}
            if len(self.data["slos"]) >= self.max_slos:
                return {"status": "SLO_LIMIT_REACHED", "saved": False}
            self.data["slos"][slo_id] = {
                "slo_id": slo_id, "service_id": service_id,
                "target_success_rate": target, "window_seconds": window_seconds,
                "min_samples": min_samples, "max_p95_latency_ms": latency,
                "status": "ACTIVE", "observations": [],
            }
        return {"status": "SLO_CREATED", "saved": True, "slo_id": slo_id}

    def set_status(self, *, slo_id: str, status: str) -> dict[str, Any]:
        if not self._valid_id(slo_id, 128):
            return {"status": "INVALID_SLO_ID", "saved": False}
        if status not in {"ACTIVE", "PAUSED"}:
            return {"status": "INVALID_SLO_STATUS", "saved": False}
        with self._transaction():
            slo = self.data["slos"].get(slo_id)
            if not isinstance(slo, dict):
                return {"status": "SLO_NOT_FOUND", "saved": False}
            slo["status"] = status
        return {"status": "SLO_STATUS_UPDATED", "saved": True}

    def record_observation(
        self,
        *,
        slo_id: str,
        service_id: str,
        request_id: str,
        success: bool,
        latency_ms: float,
    ) -> dict[str, Any]:
        latency = finite_number(latency_ms, minimum=0)
        if not self._valid_id(slo_id, 128) or not self._valid_id(service_id, 128) or not self._valid_id(request_id, 200):
            return {"status": "INVALID_SLO_OBSERVATION_ID", "saved": False}
        if not isinstance(success, bool) or latency is None:
            return {"status": "INVALID_SLO_OBSERVATION", "saved": False}
        try:
            now = self._now().isoformat()
        except Exception:
            return {"status": "SLO_CLOCK_FAILED", "saved": False}
        with self._transaction():
            slo = self.data["slos"].get(slo_id)
            if not isinstance(slo, dict):
                return {"status": "SLO_NOT_FOUND", "saved": False}
            if slo["service_id"] != service_id:
                return {"status": "SERVICE_SCOPE_MISMATCH", "saved": False}
            if slo["status"] != "ACTIVE":
                return {"status": "SLO_NOT_ACTIVE", "saved": False}
            existing = next((item for item in slo["observations"] if item["request_id"] == request_id), None)
            candidate = {"request_id": request_id, "observed_at": now, "success": success, "latency_ms": latency}
            if existing is not None:
                if existing["success"] == success and existing["latency_ms"] == latency:
                    return {"status": "ALREADY_RECORDED", "saved": True}
                return {"status": "OBSERVATION_CONFLICT", "saved": False}
            if len(slo["observations"]) >= self.max_observations:
                return {"status": "OBSERVATION_LIMIT_REACHED", "saved": False}
            slo["observations"].append(candidate)
        return {"status": "SLO_OBSERVATION_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def evaluate(self, *, slo_id: str, service_id: str) -> dict[str, Any]:
        if not self._valid_id(slo_id, 128) or not self._valid_id(service_id, 128):
            return {"status": "INVALID_SLO_SCOPE", "slo_status": "UNKNOWN"}
        try:
            now = self._now()
        except Exception:
            return {"status": "SLO_CLOCK_FAILED", "slo_status": "UNKNOWN"}
        with self._transaction():
            slo = self.data["slos"].get(slo_id)
            if not isinstance(slo, dict):
                return {"status": "SLO_NOT_FOUND", "slo_status": "UNKNOWN"}
            if slo["service_id"] != service_id:
                return {"status": "SERVICE_SCOPE_MISMATCH", "slo_status": "UNKNOWN"}
            cutoff = now - timedelta(seconds=slo["window_seconds"])
            observations = [
                item for item in slo["observations"]
                if cutoff <= self._timestamp(item["observed_at"]) <= now
            ]
            count = len(observations)
            if slo["status"] != "ACTIVE":
                state = "PAUSED"
            elif count < slo["min_samples"]:
                state = "INSUFFICIENT_DATA"
            else:
                success_rate = sum(1 for item in observations if item["success"]) / count
                p95 = self._percentile([item["latency_ms"] for item in observations], 0.95)
                breached = success_rate < slo["target_success_rate"] or (
                    slo["max_p95_latency_ms"] is not None and p95 is not None
                    and p95 > slo["max_p95_latency_ms"]
                )
                state = "SLO_BREACHED" if breached else "SLO_MET"
            successes = sum(1 for item in observations if item["success"])
            failures = count - successes
            success_rate = successes / count if count else None
            p95 = self._percentile([item["latency_ms"] for item in observations], 0.95)
            allowed_failures = max(0.0, (1.0 - slo["target_success_rate"]) * count)
            return {
                "status": "SLO_EVALUATED", "slo_status": state, "slo_id": slo_id,
                "service_id": service_id, "window_seconds": slo["window_seconds"],
                "sample_count": count, "success_count": successes, "failure_count": failures,
                "success_rate": success_rate, "target_success_rate": slo["target_success_rate"],
                "p95_latency_ms": p95, "max_p95_latency_ms": slo["max_p95_latency_ms"],
                "error_budget_allowed_failures": allowed_failures,
                "error_budget_remaining": max(0.0, allowed_failures - failures),
            }
