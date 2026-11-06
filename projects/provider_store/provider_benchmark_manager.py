"""Bounded provider/model benchmark samples with optional durable storage."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

_SCHEMA_VERSION = 1
_MAX_STATE_BYTES = 16 * 1024 * 1024
_SAMPLE_FIELDS = {
    "provider_id", "model_id", "modality", "run_id", "success",
    "latency_ms", "actual_cost", "quality_score", "recorded_at",
}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


class ProviderBenchmarkManager:
    """Collect bounded benchmark data; persist only explicitly enabled, safe summaries.

    With no storage_path, behavior remains in-memory and process-local. When a path is
    configured, every read/write reloads state under an OS-level lock and writes via
    same-directory atomic replacement. This coordinates workers sharing a filesystem;
    it is not a distributed database or a claim of multi-host consistency.
    """

    def __init__(
        self,
        *,
        max_samples: int = 5000,
        storage_path: str | os.PathLike[str] | None = None,
        clock: Any = None,
        minimum_samples_for_confidence: int = 10,
    ) -> None:
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 100000:
            raise ValueError("max_samples is outside safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if (
            isinstance(minimum_samples_for_confidence, bool)
            or not isinstance(minimum_samples_for_confidence, int)
            or not 1 <= minimum_samples_for_confidence <= 100000
        ):
            raise ValueError("minimum_samples_for_confidence is outside safe limits")
        if storage_path is not None:
            if not isinstance(storage_path, (str, os.PathLike)) or not str(storage_path).strip():
                raise ValueError("storage_path must be a non-empty path")
            try:
                resolved_path = Path(storage_path)
            except (TypeError, ValueError, OSError) as exc:
                raise ValueError("storage_path is invalid") from exc
        else:
            resolved_path = None

        self.max_samples = max_samples
        self.minimum_samples_for_confidence = minimum_samples_for_confidence
        self.clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self.storage_path = resolved_path
        self._lock_path = (
            resolved_path.with_name(resolved_path.name + ".lock")
            if resolved_path is not None else None
        )
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._lock = threading.RLock()

    @staticmethod
    def _valid_identifier(value: Any, *, maximum: int) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum

    @staticmethod
    def _normalize_timestamp(value: Any) -> str | None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        try:
            return value.astimezone(timezone.utc).isoformat()
        except (OverflowError, ValueError):
            return None

    @classmethod
    def _validate_sample(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or set(value) != _SAMPLE_FIELDS:
            return None
        for key, maximum in (
            ("provider_id", 128), ("model_id", 256),
            ("modality", 64), ("run_id", 200),
        ):
            if not cls._valid_identifier(value.get(key), maximum=maximum):
                return None
        if not isinstance(value.get("success"), bool):
            return None
        latency = finite_number(value.get("latency_ms"), minimum=0)
        cost = finite_number(value.get("actual_cost"), minimum=0)
        raw_quality = value.get("quality_score")
        quality = None if raw_quality is None else finite_number(raw_quality, minimum=0)
        if (
            latency is None or cost is None
            or (raw_quality is not None and (quality is None or quality > 1))
        ):
            return None
        recorded_at = value.get("recorded_at")
        if not isinstance(recorded_at, str) or len(recorded_at) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(recorded_at)
        except ValueError:
            return None
        normalized_timestamp = cls._normalize_timestamp(parsed)
        if normalized_timestamp is None:
            return None
        return {
            "provider_id": value["provider_id"],
            "model_id": value["model_id"],
            "modality": value["modality"],
            "run_id": value["run_id"],
            "success": value["success"],
            "latency_ms": latency,
            "actual_cost": cost,
            "quality_score": quality,
            "recorded_at": normalized_timestamp,
        }

    def _read_persisted_unlocked(self) -> list[dict[str, Any]]:
        assert self.storage_path is not None
        try:
            state_size = self.storage_path.stat().st_size
        except FileNotFoundError:
            return []
        if state_size > _MAX_STATE_BYTES:
            raise ValueError("benchmark state exceeds size limit")
        try:
            raw = self.storage_path.read_bytes()
        except FileNotFoundError:
            return []
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError("benchmark state exceeds size limit")
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("benchmark state is invalid") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "samples"}
            or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != _SCHEMA_VERSION
            or not isinstance(payload.get("samples"), list)
            or len(payload["samples"]) > self.max_samples
        ):
            raise ValueError("benchmark state schema is invalid")
        samples: list[dict[str, Any]] = []
        for item in payload["samples"]:
            validated = self._validate_sample(item)
            if validated is None:
                raise ValueError("benchmark sample is invalid")
            samples.append(validated)
        return samples

    def _write_persisted_unlocked(self, samples: list[dict[str, Any]]) -> None:
        assert self.storage_path is not None
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            {"schema_version": _SCHEMA_VERSION, "samples": samples},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8") + b"\n"
        if len(encoded) > _MAX_STATE_BYTES:
            raise ValueError("benchmark state exceeds size limit")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.storage_path.name}.",
            suffix=".tmp",
            dir=str(self.storage_path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary_name, self.storage_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def _timestamp_now(self) -> str | None:
        try:
            return self._normalize_timestamp(self.clock())
        except Exception:
            return None

    def record(
        self, *, provider_id: str, model_id: str, modality: str, run_id: str,
        success: bool, latency_ms: float, actual_cost: float,
        quality_score: float | None = None,
    ) -> dict[str, Any]:
        for value, name, limit in (
            (provider_id, "provider_id", 128),
            (model_id, "model_id", 256),
            (modality, "modality", 64),
            (run_id, "run_id", 200),
        ):
            if not self._valid_identifier(value, maximum=limit):
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        cost = finite_number(actual_cost, minimum=0)
        quality = None if quality_score is None else finite_number(quality_score, minimum=0)
        if latency is None or cost is None or (
            quality_score is not None and (quality is None or quality > 1)
        ):
            return {"status": "INVALID_BENCHMARK_METRIC", "saved": False}
        recorded_at = self._timestamp_now()
        if recorded_at is None:
            return {"status": "INVALID_BENCHMARK_TIMESTAMP", "saved": False}
        sample = {
            "provider_id": provider_id,
            "model_id": model_id,
            "modality": modality,
            "run_id": run_id,
            "success": success,
            "latency_ms": latency,
            "actual_cost": cost,
            "quality_score": quality,
            "recorded_at": recorded_at,
        }
        with self._lock:
            if self.storage_path is None:
                self._samples.append(sample)
                return {"status": "BENCHMARK_SAMPLE_RECORDED", "saved": True}
            assert self._lock_path is not None
            try:
                with interprocess_file_lock(self._lock_path):
                    samples = self._read_persisted_unlocked()
                    samples.append(sample)
                    samples = samples[-self.max_samples:]
                    self._write_persisted_unlocked(samples)
            except ValueError:
                return {"status": "BENCHMARK_STATE_INVALID", "saved": False}
            except Exception as exc:
                return {
                    "status": "BENCHMARK_STATE_WRITE_FAILED",
                    "saved": False,
                    "error_type": type(exc).__name__,
                }
        return {"status": "BENCHMARK_SAMPLE_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        return values[max(0, math.ceil(p * len(values)) - 1)]

    @staticmethod
    def _wilson_interval(successes: int, total: int) -> tuple[float, float] | None:
        if total < 1:
            return None
        z = 1.96
        proportion = successes / total
        denominator = 1.0 + (z * z / total)
        center = (proportion + (z * z / (2.0 * total))) / denominator
        margin = (
            z * math.sqrt(
                (proportion * (1.0 - proportion) / total)
                + (z * z / (4.0 * total * total))
            ) / denominator
        )
        return max(0.0, center - margin), min(1.0, center + margin)

    def compare(self, *, modality: str | None = None) -> dict[str, Any]:
        if modality is not None and not self._valid_identifier(modality, maximum=64):
            return {"status": "INVALID_MODALITY", "results": []}
        with self._lock:
            if self.storage_path is None:
                samples = [
                    dict(sample) for sample in self._samples
                    if modality is None or sample["modality"] == modality
                ]
            else:
                assert self._lock_path is not None
                try:
                    with interprocess_file_lock(self._lock_path):
                        persisted = self._read_persisted_unlocked()
                except ValueError:
                    return {"status": "BENCHMARK_STATE_INVALID", "results": []}
                except Exception as exc:
                    return {
                        "status": "BENCHMARK_STATE_READ_FAILED",
                        "results": [],
                        "error_type": type(exc).__name__,
                    }
                samples = [
                    dict(sample) for sample in persisted
                    if modality is None or sample["modality"] == modality
                ]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for sample in samples:
            groups.setdefault(
                (sample["provider_id"], sample["model_id"], sample["modality"]), []
            ).append(sample)
        results = []
        for (provider_id, model_id, sample_modality), group in groups.items():
            successes = [sample for sample in group if sample["success"]]
            latencies = [sample["latency_ms"] for sample in group]
            costs = [sample["actual_cost"] for sample in group]
            qualities = [
                sample["quality_score"] for sample in group
                if sample["quality_score"] is not None
            ]
            interval = self._wilson_interval(len(successes), len(group))
            sample_count = len(group)
            results.append({
                "provider_id": provider_id,
                "model_id": model_id,
                "modality": sample_modality,
                "sample_count": sample_count,
                "success_count": len(successes),
                "success_rate": len(successes) / sample_count,
                "success_rate_ci_95": (
                    {"lower": interval[0], "upper": interval[1]}
                    if interval is not None else None
                ),
                "sample_confidence": (
                    "MEETS_MINIMUM_SAMPLE_COUNT"
                    if sample_count >= self.minimum_samples_for_confidence
                    else "INSUFFICIENT_SAMPLES"
                ),
                "minimum_samples_for_confidence": self.minimum_samples_for_confidence,
                "quality_sample_count": len(qualities),
                "p50_latency_ms": self._percentile(latencies, 0.50),
                "p95_latency_ms": self._percentile(latencies, 0.95),
                "avg_cost_per_attempt": sum(costs) / len(costs),
                "avg_cost_per_success": (
                    sum(sample["actual_cost"] for sample in successes) / len(successes)
                    if successes else None
                ),
                "avg_quality_score": (
                    sum(qualities) / len(qualities) if qualities else None
                ),
            })
        results.sort(key=lambda item: (
            -item["success_rate"],
            -(item["avg_quality_score"] if item["avg_quality_score"] is not None else -1.0),
            item["p95_latency_ms"] if item["p95_latency_ms"] is not None else float("inf"),
            item["avg_cost_per_success"] if item["avg_cost_per_success"] is not None else float("inf"),
            item["provider_id"], item["model_id"],
        ))
        return {
            "status": "BENCHMARKS_AVAILABLE" if results else "NO_BENCHMARKS",
            "results": results,
        }
