"""Bounded provider request metrics with safe snapshots and optional persistence."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import uuid
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderMetricsManager:
    def __init__(
        self,
        ledger_path: str | os.PathLike[str] | None = None,
        *,
        max_events: int = 10_000,
    ) -> None:
        if ledger_path is not None and (
            not isinstance(ledger_path, (str, os.PathLike)) or not str(ledger_path).strip()
        ):
            raise ValueError("ledger_path must be a valid path or None")
        if not isinstance(max_events, int) or isinstance(max_events, bool) or not 1 <= max_events <= 1_000_000:
            raise ValueError("max_events must be between 1 and 1000000")
        self.path = Path(ledger_path) if ledger_path is not None else None
        self.max_events = max_events
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "events": [], "seen": {}}
        self._reload()

    @staticmethod
    def _valid_nonnegative(value: Any, *, maximum: float = 86_400_000.0) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= maximum
        )

    def _reload(self) -> None:
        if self.path is None or not self.path.exists():
            self._data = {"version": 1, "events": [], "seen": {}}
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid provider metrics ledger") from exc
        if (
            not isinstance(loaded, dict) or loaded.get("version") != 1
            or not isinstance(loaded.get("events"), list)
            or not isinstance(loaded.get("seen"), dict)
            or len(loaded["events"]) > self.max_events
        ):
            raise ValueError("Invalid provider metrics ledger schema")
        event_ids = set()
        for event in loaded["events"]:
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("event_id"), str) or len(event["event_id"]) != 64
                or any(ch not in "0123456789abcdef" for ch in event["event_id"])
                or not isinstance(event.get("provider_id"), str) or not event["provider_id"].strip()
                or not isinstance(event.get("success"), bool)
                or (
                    event.get("latency_ms") is not None
                    and not self._valid_nonnegative(event.get("latency_ms"))
                )
                or not self._valid_nonnegative(event.get("queue_wait_ms"))
                or not isinstance(event.get("timed_out"), bool)
                or (event["timed_out"] and event["success"])
                or not self._valid_nonnegative(event.get("timestamp"), maximum=10_000_000_000.0)
                or (
                    event.get("request_id") is not None
                    and (
                        not isinstance(event.get("request_id"), str)
                        or not event["request_id"].strip() or len(event["request_id"]) > 200
                    )
                )
            ):
                raise ValueError("Invalid provider metrics event")
            if event["event_id"] in event_ids:
                raise ValueError("Duplicate provider metrics event ID")
            event_ids.add(event["event_id"])
        if any(not isinstance(key, str) or not isinstance(value, str) or value not in event_ids for key, value in loaded["seen"].items()):
            raise ValueError("Invalid provider metrics idempotency index")
        self._data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            if self.path is None:
                yield
            else:
                lock_path = self.path.with_name(self.path.name + ".lock")
                with interprocess_file_lock(lock_path):
                    self._reload()
                    yield

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._data, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def record_request(
        self,
        provider_id: str,
        *,
        success: bool,
        latency_ms: float | None,
        queue_wait_ms: float = 0.0,
        timed_out: bool = False,
        request_id: str | None = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 200:
            return {"status": "INVALID_PROVIDER", "saved": False}
        if not isinstance(success, bool) or not isinstance(timed_out, bool) or (success and timed_out):
            return {"status": "INVALID_OUTCOME", "saved": False}
        if (
            (latency_ms is not None and not self._valid_nonnegative(latency_ms))
            or not self._valid_nonnegative(queue_wait_ms)
        ):
            return {"status": "INVALID_METRIC_VALUE", "saved": False}
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200
        ):
            return {"status": "INVALID_REQUEST_ID", "saved": False}
        when = time.time() if timestamp is None else timestamp
        if not self._valid_nonnegative(when, maximum=10_000_000_000.0):
            return {"status": "INVALID_TIMESTAMP", "saved": False}
        dedupe_key = None
        if request_id is not None:
            dedupe_key = hashlib.sha256(
                provider_id.encode("utf-8") + b"\0" + request_id.encode("utf-8")
            ).hexdigest()
        with self._transaction():
            if dedupe_key is not None and dedupe_key in self._data["seen"]:
                return {"status": "DUPLICATE_METRIC_EVENT", "saved": False}
            material = (
                f"{provider_id}\0{request_id or ''}\0{when:.9f}\0{len(self._data['events'])}\0{uuid.uuid4().hex}"
            ).encode("utf-8")
            event_id = hashlib.sha256(material).hexdigest()
            previous_events = list(self._data["events"])
            previous_seen = dict(self._data["seen"])
            event = {
                "event_id": event_id,
                "provider_id": provider_id,
                "success": success,
                "latency_ms": None if latency_ms is None else float(latency_ms),
                "queue_wait_ms": float(queue_wait_ms),
                "timed_out": timed_out,
                "request_id": request_id,
                "timestamp": float(when),
            }
            self._data["events"].append(event)
            self._data["events"] = self._data["events"][-self.max_events:]
            retained_ids = {item["event_id"] for item in self._data["events"]}
            self._data["seen"] = {
                key: value for key, value in self._data["seen"].items() if value in retained_ids
            }
            if dedupe_key is not None:
                self._data["seen"][dedupe_key] = event_id
            try:
                self._save()
            except Exception as exc:
                self._data["events"] = previous_events
                self._data["seen"] = previous_seen
                return {"status": "METRICS_PERSISTENCE_FAILED", "saved": False, "error_type": type(exc).__name__}
        return {"status": "RECORDED", "saved": True, "event_id": event_id}

    def snapshot(
        self,
        provider_id: str,
        *,
        window_seconds: int = 300,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 200:
            return {"status": "INVALID_PROVIDER"}
        if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or not 1 <= window_seconds <= 2_592_000:
            return {"status": "INVALID_WINDOW"}
        current = time.time() if now is None else now
        if not self._valid_nonnegative(current, maximum=10_000_000_000.0):
            return {"status": "INVALID_CLOCK"}
        with self._transaction():
            events = [
                dict(event) for event in self._data["events"]
                if event["provider_id"] == provider_id
                and current - window_seconds <= event["timestamp"] <= current
            ]
        events.sort(key=lambda event: event["timestamp"])
        total = len(events)
        successes = sum(1 for event in events if event["success"])
        timeouts = sum(1 for event in events if event["timed_out"])
        latencies = sorted(event["latency_ms"] for event in events if event["latency_ms"] is not None)
        queues = [event["queue_wait_ms"] for event in events]
        p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1) if latencies else 0
        return {
            "status": "OK",
            "provider_id": provider_id,
            "window_seconds": window_seconds,
            "request_count": total,
            "success_count": successes,
            "failure_count": total - successes,
            "timeout_count": timeouts,
            "success_rate_pct": round(successes * 100.0 / total, 4) if total else None,
            "average_latency_ms": round(sum(latencies) / len(latencies), 4) if latencies else None,
            "p95_latency_ms": latencies[p95_index] if latencies else None,
            "average_queue_wait_ms": round(sum(queues) / total, 4) if total else None,
            "throughput_per_minute": round(total * 60.0 / window_seconds, 4),
            "last_event_at": events[-1]["timestamp"] if events else None,
        }

    def list_events(self, provider_id: str | None = None) -> dict[str, Any]:
        if provider_id is not None and (
            not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 200
        ):
            return {"status": "INVALID_PROVIDER", "events": []}
        with self._transaction():
            events = [
                dict(event) for event in self._data["events"]
                if provider_id is None or event["provider_id"] == provider_id
            ]
        return {"status": "OK", "events": events}
