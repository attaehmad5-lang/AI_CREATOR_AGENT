"""Persistent rolling-window service-level objective tracking.

Only bounded operational metadata is stored. Prompts, payloads, credentials, and
provider response bodies are deliberately excluded from samples.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_STATES = {"SLO_MET", "SLO_BREACHED", "INSUFFICIENT_DATA"}


class ProviderSLOManager:
    """Persist SLO policies and bounded observations; evaluate rolling availability and latency."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_samples: int = 100_000,
        clock: Any = None,
    ) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 1_000_000:
            raise ValueError("max_samples must be within safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_samples = max_samples
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": "1.0", "objectives": {}, "samples": [],
        }
        self._reload()

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "objectives": {}, "samples": []}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("invalid SLO state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("objectives"), dict)
            or not isinstance(loaded.get("samples"), list)
            or len(loaded["samples"]) > self.max_samples
        ):
            raise ValueError("invalid SLO state schema or capacity")
        for slo_id, objective in loaded["objectives"].items():
            if not self._valid_objective(slo_id, objective):
                raise ValueError("invalid SLO objective record")
        for sample in loaded["samples"]:
            if not self._valid_sample(sample):
                raise ValueError("invalid SLO observation record")
        self.data = loaded

    @staticmethod
    def _valid_objective(slo_id: Any, objective: Any) -> bool:
        if (
            not isinstance(slo_id, str) or not _ID_RE.fullmatch(slo_id)
            or not isinstance(objective, dict)
            or objective.get("slo_id") != slo_id
            or not isinstance(objective.get("provider_id"), str)
            or not _ID_RE.fullmatch(objective["provider_id"])
            or not isinstance(objective.get("category"), str)
            or not _ID_RE.fullmatch(objective["category"])
            or isinstance(objective.get("availability_target"), bool)
            or not isinstance(objective.get("availability_target"), (int, float))
            or not math.isfinite(objective["availability_target"])
            or not 0 < objective["availability_target"] <= 1
            or isinstance(objective.get("max_p95_latency_ms"), bool)
            or not isinstance(objective.get("max_p95_latency_ms"), (int, float))
            or not math.isfinite(objective["max_p95_latency_ms"])
            or objective["max_p95_latency_ms"] <= 0
            or isinstance(objective.get("window_seconds"), bool)
            or not isinstance(objective.get("window_seconds"), int)
            or not 60 <= objective["window_seconds"] <= 31_536_000
            or isinstance(objective.get("minimum_samples"), bool)
            or not isinstance(objective.get("minimum_samples"), int)
            or not 1 <= objective["minimum_samples"] <= 1_000_000
        ):
            return False
        return True

    @staticmethod
    def _valid_sample(sample: Any) -> bool:
        return (
            isinstance(sample, dict)
            and isinstance(sample.get("provider_id"), str)
            and _ID_RE.fullmatch(sample["provider_id"]) is not None
            and isinstance(sample.get("category"), str)
            and _ID_RE.fullmatch(sample["category"]) is not None
            and isinstance(sample.get("success"), bool)
            and isinstance(sample.get("latency_ms"), (int, float))
            and not isinstance(sample.get("latency_ms"), bool)
            and math.isfinite(sample["latency_ms"])
            and sample["latency_ms"] >= 0
            and ProviderSLOManager._parse_timestamp(sample.get("recorded_at")) is not None
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    self.data, stream, ensure_ascii=False, indent=2,
                    sort_keys=True, allow_nan=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def configure(
        self,
        slo_id: str,
        *,
        provider_id: str,
        category: str,
        availability_target: float,
        max_p95_latency_ms: float,
        window_seconds: int = 3600,
        minimum_samples: int = 20,
    ) -> dict[str, Any]:
        for value, name in ((slo_id, "slo_id"), (provider_id, "provider_id"), (category, "category")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        target = finite_number(availability_target, minimum=0)
        latency = finite_number(max_p95_latency_ms, minimum=0)
        if target is None or target <= 0 or target > 1:
            return {"status": "INVALID_AVAILABILITY_TARGET", "saved": False}
        if latency is None or latency <= 0:
            return {"status": "INVALID_LATENCY_TARGET", "saved": False}
        if (
            isinstance(window_seconds, bool) or not isinstance(window_seconds, int)
            or not 60 <= window_seconds <= 31_536_000
        ):
            return {"status": "INVALID_WINDOW", "saved": False}
        if (
            isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int)
            or not 1 <= minimum_samples <= 1_000_000
        ):
            return {"status": "INVALID_MINIMUM_SAMPLES", "saved": False}
        objective = {
            "slo_id": slo_id,
            "provider_id": provider_id,
            "category": category,
            "availability_target": target,
            "max_p95_latency_ms": latency,
            "window_seconds": window_seconds,
            "minimum_samples": minimum_samples,
        }
        with self._transaction():
            self.data["objectives"][slo_id] = objective
            self._save()
        return {"status": "SLO_CONFIGURED", "saved": True, "objective": dict(objective)}

    def remove(self, slo_id: str) -> dict[str, Any]:
        if not isinstance(slo_id, str) or not _ID_RE.fullmatch(slo_id):
            return {"status": "INVALID_SLO_ID", "removed": False}
        with self._transaction():
            if slo_id not in self.data["objectives"]:
                return {"status": "SLO_NOT_FOUND", "removed": False}
            del self.data["objectives"][slo_id]
            self._save()
        return {"status": "SLO_REMOVED", "removed": True}

    def record(
        self,
        *,
        provider_id: str,
        category: str,
        success: bool,
        latency_ms: float,
    ) -> dict[str, Any]:
        for value, name in ((provider_id, "provider_id"), (category, "category")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        if latency is None:
            return {"status": "INVALID_LATENCY", "saved": False}
        try:
            recorded_at = self.clock().astimezone(timezone.utc).isoformat()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "saved": False}
        sample = {
            "provider_id": provider_id,
            "category": category,
            "success": success,
            "latency_ms": latency,
            "recorded_at": recorded_at,
        }
        with self._transaction():
            self.data["samples"].append(sample)
            if len(self.data["samples"]) > self.max_samples:
                del self.data["samples"][:len(self.data["samples"]) - self.max_samples]
            self._save()
        return {"status": "SLO_OBSERVATION_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def _evaluate_locked(self, objective: dict[str, Any], now: datetime) -> dict[str, Any]:
        cutoff = now - timedelta(seconds=objective["window_seconds"])
        samples: list[dict[str, Any]] = []
        for item in self.data["samples"]:
            if item["provider_id"] != objective["provider_id"] or item["category"] != objective["category"]:
                continue
            timestamp = self._parse_timestamp(item["recorded_at"])
            if timestamp is not None and cutoff <= timestamp <= now:
                samples.append(item)
        count = len(samples)
        success_count = sum(item["success"] for item in samples)
        failure_count = count - success_count
        success_rate = success_count / count if count else None
        p95 = self._percentile([item["latency_ms"] for item in samples], 0.95)
        allowed_failures = (1.0 - objective["availability_target"]) * count
        remaining_failures = max(0.0, allowed_failures - failure_count)
        budget_remaining_percent = (
            100.0 if allowed_failures == 0 and failure_count == 0
            else 0.0 if allowed_failures == 0
            else max(0.0, min(100.0, remaining_failures / allowed_failures * 100.0))
        )
        if count < objective["minimum_samples"]:
            status = "INSUFFICIENT_DATA"
            availability_met = None
            latency_met = None
        else:
            availability_met = success_rate >= objective["availability_target"]
            latency_met = p95 <= objective["max_p95_latency_ms"]
            status = "SLO_MET" if availability_met and latency_met else "SLO_BREACHED"
        return {
            "status": status,
            "slo_id": objective["slo_id"],
            "provider_id": objective["provider_id"],
            "category": objective["category"],
            "window_seconds": objective["window_seconds"],
            "sample_count": count,
            "minimum_samples": objective["minimum_samples"],
            "availability_target": objective["availability_target"],
            "availability": success_rate,
            "availability_met": availability_met,
            "max_p95_latency_ms": objective["max_p95_latency_ms"],
            "p95_latency_ms": p95,
            "latency_met": latency_met,
            "failure_count": failure_count,
            "allowed_failures": round(allowed_failures, 4),
            "remaining_error_budget": round(remaining_failures, 4),
            "error_budget_remaining_percent": round(budget_remaining_percent, 2),
            "evaluated_at": now.isoformat(),
        }

    def evaluate(self, slo_id: str) -> dict[str, Any]:
        if not isinstance(slo_id, str) or not _ID_RE.fullmatch(slo_id):
            return {"status": "INVALID_SLO_ID", "evaluation": None}
        try:
            now = self.clock().astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "evaluation": None}
        with self._transaction():
            objective = self.data["objectives"].get(slo_id)
            if objective is None:
                return {"status": "SLO_NOT_FOUND", "evaluation": None}
            evaluation = self._evaluate_locked(objective, now)
            return {"status": evaluation["status"], "evaluation": evaluation}

    def evaluate_all(self) -> dict[str, Any]:
        try:
            now = self.clock().astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "evaluations": []}
        with self._transaction():
            evaluations = [
                self._evaluate_locked(objective, now)
                for _, objective in sorted(self.data["objectives"].items())
            ]
        return {
            "status": "SLO_EVALUATIONS_AVAILABLE" if evaluations else "NO_SLOS",
            "evaluations": evaluations,
        }
