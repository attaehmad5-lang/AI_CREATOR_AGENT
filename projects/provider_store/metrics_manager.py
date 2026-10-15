"""Privacy-conscious, bounded provider execution metrics with durable idempotency."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


DEFAULT_METRICS_PATH = Path(__file__).with_name("provider_metrics.json")


class ProviderMetricsManager:
    """Persist only safe execution metadata; never store prompts, outputs, or error text."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_events: int = 10000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(max_events, int)
            or isinstance(max_events, bool)
            or not 1 <= max_events <= 100000
        ):
            raise ValueError("max_events must be an integer between 1 and 100000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path) if path is not None else DEFAULT_METRICS_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_events = max_events
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load_unlocked()
        self._validate_document(self.data)

    @staticmethod
    def _valid_id(value: Any, *, max_length: int = 200) -> bool:
        return isinstance(value, str) and bool(
            re.fullmatch(rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{max_length - 1}}}", value)
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid metrics timestamp")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid metrics timestamp") from exc
        if result.tzinfo is None:
            raise ValueError("metrics timestamp must include timezone")
        return result.astimezone(timezone.utc)

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("events"), dict):
            raise ValueError("invalid provider metrics document")
        for request_id, event in data["events"].items():
            if not cls._valid_id(request_id) or not isinstance(event, dict):
                raise ValueError("invalid provider metrics event")
            if (
                event.get("request_id") != request_id
                or not cls._valid_id(event.get("trace_id"))
                or not cls._valid_id(event.get("provider_id"), max_length=120)
                or not isinstance(event.get("modality"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", event.get("modality", ""))
                or not isinstance(event.get("status"), str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", event.get("status", ""))
                or not isinstance(event.get("success"), bool)
                or finite_number(event.get("latency_ms"), minimum=0) is None
            ):
                raise ValueError("invalid provider metrics event")
            cls._parse_timestamp(event.get("recorded_at"))

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "events": {}}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("provider metrics store is unreadable") from exc
        self._validate_document(data)
        return data

    def _save_unlocked(self) -> None:
        self._validate_document(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def record_request(
        self,
        *,
        request_id: str,
        trace_id: str,
        provider_id: str,
        modality: str,
        status: str,
        success: bool,
        latency_ms: float,
    ) -> dict[str, Any]:
        if not self._valid_id(request_id):
            return {"status": "INVALID_REQUEST_ID", "saved": False}
        if not self._valid_id(trace_id):
            return {"status": "INVALID_TRACE_ID", "saved": False}
        if not self._valid_id(provider_id, max_length=120):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not isinstance(modality, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", modality):
            return {"status": "INVALID_MODALITY", "saved": False}
        if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", status):
            return {"status": "INVALID_STATUS", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_SUCCESS_FLAG", "saved": False}
        latency = finite_number(latency_ms, minimum=0)
        if latency is None:
            return {"status": "INVALID_LATENCY", "saved": False}
        try:
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                raise ValueError("metrics clock must return timezone-aware datetime")
            timestamp = now.astimezone(timezone.utc).isoformat()
        except Exception:
            return {"status": "INVALID_METRICS_CLOCK", "saved": False}
        event = {
            "request_id": request_id,
            "trace_id": trace_id,
            "provider_id": provider_id,
            "modality": modality,
            "status": status,
            "success": success,
            "latency_ms": latency,
            "recorded_at": timestamp,
        }
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_unlocked()
            existing = self.data["events"].get(request_id)
            if existing is not None:
                if existing == event:
                    return {"status": "ALREADY_RECORDED", "saved": True, "duplicate": True}
                return {"status": "REQUEST_ID_CONFLICT", "saved": False}
            self.data["events"][request_id] = event
            if len(self.data["events"]) > self.max_events:
                ordered = sorted(
                    self.data["events"].items(),
                    key=lambda item: self._parse_timestamp(item[1]["recorded_at"]),
                )
                for old_request_id, _ in ordered[:len(self.data["events"]) - self.max_events]:
                    del self.data["events"][old_request_id]
            self._save_unlocked()
        return {"status": "METRIC_RECORDED", "saved": True, "request_id": request_id}

    def summary(self, *, provider_id: str | None = None, modality: str | None = None) -> dict[str, Any]:
        if provider_id is not None and not self._valid_id(provider_id, max_length=120):
            return {"status": "INVALID_PROVIDER_ID"}
        if modality is not None and (
            not isinstance(modality, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", modality)
        ):
            return {"status": "INVALID_MODALITY"}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_unlocked()
            self._validate_document(self.data)
            events = [
                event for event in self.data["events"].values()
                if (provider_id is None or event["provider_id"] == provider_id)
                and (modality is None or event["modality"] == modality)
            ]
        latencies = [float(event["latency_ms"]) for event in events]
        successes = sum(1 for event in events if event["success"])
        count = len(events)
        return {
            "status": "METRICS_SUMMARY",
            "request_count": count,
            "success_count": successes,
            "failure_count": count - successes,
            "success_rate": (successes / count) if count else None,
            "average_latency_ms": (sum(latencies) / count) if count else None,
            "max_latency_ms": max(latencies) if latencies else None,
        }

    def list_events(self, *, limit: int = 100) -> dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            return {"status": "INVALID_LIMIT", "events": []}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_unlocked()
            self._validate_document(self.data)
            events = sorted(
                self.data["events"].values(),
                key=lambda event: self._parse_timestamp(event["recorded_at"]),
                reverse=True,
            )[:limit]
            return {"status": "METRICS_EVENTS", "events": copy.deepcopy(events)}
