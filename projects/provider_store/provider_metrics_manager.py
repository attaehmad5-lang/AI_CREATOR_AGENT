"""Bounded, optionally persistent provider success, failure and latency metrics."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

_PROVIDER_ID_MAX = 128
_CATEGORY_MAX = 64
_MAX_PERSISTED_SAMPLES = 100_000
_MAX_STATE_BYTES = 32 * 1024 * 1024
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\\Z")
_SECRET_MARKER_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|"
    r"password|secret|private[_-]?key|credential|jwt)"
)
_SAMPLE_FIELDS = frozenset({
    "provider_id", "category", "success", "latency_ms",
    "queue_wait_ms", "timed_out", "recorded_at",
})


class ProviderMetricsManager:
    """Aggregate bounded metrics without storing prompts, payloads, headers or tokens.

    Without `state_path`, metrics remain in-process as before. When a path is
    configured, every write and snapshot reloads the shared file while holding
    an OS-level lock. Writes use same-directory atomic replacement. The optional
    JSON file is suitable only for workers sharing a filesystem with reliable
    advisory-lock semantics; distributed deployments should use a transactional
    metrics backend and export to their telemetry system.
    """

    def __init__(
        self,
        *,
        max_samples: int = 2000,
        clock: Any = None,
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if (
            isinstance(max_samples, bool)
            or not isinstance(max_samples, int)
            or not 1 <= max_samples <= _MAX_PERSISTED_SAMPLES
        ):
            raise ValueError("max_samples must be within safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if state_path is not None:
            try:
                raw_path = os.fspath(state_path)
            except TypeError as exc:
                raise ValueError("state_path must be path-like") from exc
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("state_path must be a non-empty path")
            self.state_path: Path | None = Path(raw_path)
        else:
            self.state_path = None
        self.max_samples = max_samples
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        if self.state_path is not None:
            try:
                with self._lock, interprocess_file_lock(str(self.state_path) + ".lock"):
                    self._reload_locked()
            except (OSError, ValueError) as exc:
                raise ValueError("provider metrics state is invalid or unavailable") from exc

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _valid_label(value: Any, *, max_length: int) -> bool:
        return (
            isinstance(value, str)
            and value == value.strip()
            and len(value) <= max_length
            and _LABEL_RE.fullmatch(value) is not None
            and _SECRET_MARKER_RE.search(value) is None
        )

    @classmethod
    def _validate_sample(cls, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict) or set(item) != _SAMPLE_FIELDS:
            raise ValueError("invalid provider metrics sample schema")
        provider_id = item.get("provider_id")
        category = item.get("category")
        if not cls._valid_label(provider_id, max_length=_PROVIDER_ID_MAX):
            raise ValueError("invalid provider metrics provider ID")
        if not cls._valid_label(category, max_length=_CATEGORY_MAX):
            raise ValueError("invalid provider metrics category")
        if not isinstance(item.get("success"), bool) or not isinstance(item.get("timed_out"), bool):
            raise ValueError("invalid provider metrics flags")
        latency = finite_number(item.get("latency_ms"), minimum=0)
        queue_wait = finite_number(item.get("queue_wait_ms"), minimum=0)
        timestamp = cls._parse_timestamp(item.get("recorded_at"))
        if latency is None or queue_wait is None or timestamp is None:
            raise ValueError("invalid provider metrics measurement")
        return {
            "provider_id": provider_id,
            "category": category,
            "success": item["success"],
            "latency_ms": latency,
            "queue_wait_ms": queue_wait,
            "timed_out": item["timed_out"],
            "recorded_at": timestamp.isoformat(),
        }

    def _reload_locked(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            self._samples = deque(maxlen=self.max_samples)
            return
        try:
            raw = self.state_path.read_bytes()
            if len(raw) > _MAX_STATE_BYTES:
                raise ValueError("provider metrics state exceeds its size limit")
            state = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("provider metrics state is unreadable") from exc
        if (
            not isinstance(state, dict)
            or set(state) != {"version", "samples"}
            or type(state.get("version")) is not int
            or state["version"] != 1
            or not isinstance(state.get("samples"), list)
            or len(state["samples"]) > _MAX_PERSISTED_SAMPLES
        ):
            raise ValueError("provider metrics state schema is invalid")
        samples = [self._validate_sample(item) for item in state["samples"]]
        self._samples = deque(samples, maxlen=self.max_samples)

    def _save_locked(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "samples": list(self._samples)}
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
        if len(payload) > _MAX_STATE_BYTES:
            raise OSError("provider metrics state exceeds its size limit")
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.state_path)
            try:
                directory_fd = os.open(str(self.state_path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Some platforms/filesystems do not allow fsync on directories.
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def record(
        self,
        *,
        provider_id: str,
        category: str,
        success: bool,
        latency_ms: float,
        queue_wait_ms: float = 0.0,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        if not self._valid_label(provider_id, max_length=_PROVIDER_ID_MAX):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not self._valid_label(category, max_length=_CATEGORY_MAX):
            return {"status": "INVALID_CATEGORY", "saved": False}
        if not isinstance(success, bool) or not isinstance(timed_out, bool):
            return {"status": "INVALID_METRIC_FLAGS", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        queue_wait = finite_number(queue_wait_ms, minimum=0)
        if latency is None or queue_wait is None:
            return {"status": "INVALID_METRIC_TIMING", "saved": False}
        try:
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("clock must return a timezone-aware datetime")
            timestamp = now.astimezone(timezone.utc).isoformat()
        except Exception:
            return {"status": "INVALID_METRIC_CLOCK", "saved": False}
        sample = {
            "provider_id": provider_id,
            "category": category,
            "success": success,
            "latency_ms": latency,
            "queue_wait_ms": queue_wait,
            "timed_out": timed_out,
            "recorded_at": timestamp,
        }
        if self.state_path is None:
            with self._lock:
                self._samples.append(sample)
            return {"status": "METRIC_RECORDED", "saved": True}
        try:
            with self._lock, interprocess_file_lock(str(self.state_path) + ".lock"):
                self._reload_locked()
                previous = list(self._samples)
                self._samples.append(sample)
                try:
                    self._save_locked()
                except Exception:
                    self._samples = deque(previous, maxlen=self.max_samples)
                    raise
        except ValueError:
            return {"status": "METRICS_STATE_INVALID", "saved": False}
        except OSError:
            return {"status": "METRICS_PERSIST_FAILED", "saved": False}
        return {"status": "METRIC_RECORDED", "saved": True}

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, math.ceil(percentile * len(ordered)) - 1)
        return ordered[index]

    def snapshot(
        self, *, provider_id: str | None = None, category: str | None = None
    ) -> dict[str, Any]:
        if provider_id is not None and (
            not isinstance(provider_id, str) or not provider_id.strip()
        ):
            return {"status": "INVALID_PROVIDER_ID", "metrics": None}
        if category is not None and (
            not isinstance(category, str) or not category.strip()
        ):
            return {"status": "INVALID_CATEGORY", "metrics": None}
        try:
            if self.state_path is None:
                with self._lock:
                    samples = [dict(item) for item in self._samples]
            else:
                with self._lock, interprocess_file_lock(str(self.state_path) + ".lock"):
                    self._reload_locked()
                    samples = [dict(item) for item in self._samples]
        except ValueError:
            return {"status": "METRICS_STATE_INVALID", "metrics": None}
        except OSError:
            return {"status": "METRICS_STATE_UNAVAILABLE", "metrics": None}
        samples = [
            item for item in samples
            if (provider_id is None or item["provider_id"] == provider_id)
            and (category is None or item["category"] == category)
        ]
        if not samples:
            return {
                "status": "NO_METRICS",
                "metrics": {
                    "sample_count": 0, "success_count": 0, "failure_count": 0,
                    "timeout_count": 0, "success_rate": None,
                    "avg_latency_ms": None, "p50_latency_ms": None,
                    "p95_latency_ms": None, "avg_queue_wait_ms": None,
                },
            }
        latencies = [item["latency_ms"] for item in samples]
        waits = [item["queue_wait_ms"] for item in samples]
        success_count = sum(item["success"] for item in samples)
        metrics = {
            "sample_count": len(samples),
            "success_count": success_count,
            "failure_count": len(samples) - success_count,
            "timeout_count": sum(item["timed_out"] for item in samples),
            "success_rate": success_count / len(samples),
            "avg_latency_ms": sum(latencies) / len(latencies),
            "p50_latency_ms": self._percentile(latencies, 0.50),
            "p95_latency_ms": self._percentile(latencies, 0.95),
            "avg_queue_wait_ms": sum(waits) / len(waits),
        }
        return {"status": "METRICS_AVAILABLE", "metrics": metrics}
