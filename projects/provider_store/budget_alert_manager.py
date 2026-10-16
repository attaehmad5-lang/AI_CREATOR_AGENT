"""Persistent, idempotent budget-threshold alert events.

This module queues alert events for an application-owned notification delivery layer.
It never sends messages or triggers purchases on its own.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


class BudgetAlertManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        thresholds: tuple[float, ...] = (0.5, 0.8, 1.0),
        clock: Any = None,
    ) -> None:
        if not isinstance(thresholds, tuple) or not thresholds:
            raise ValueError("thresholds must be a non-empty tuple")
        normalized = []
        for value in thresholds:
            number = finite_number(value, minimum=0)
            if number is None or number <= 0 or number > 1:
                raise ValueError("budget thresholds must be finite and in (0, 1]")
            normalized.append(float(number))
        if normalized != sorted(set(normalized)):
            raise ValueError("budget thresholds must be unique and ascending")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.thresholds = tuple(normalized)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "events": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "events": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid budget alert state JSON") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("events"), dict):
            raise ValueError("invalid budget alert state schema")
        for event_id, event in loaded["events"].items():
            if (
                not isinstance(event_id, str) or not event_id
                or not isinstance(event, dict)
                or not isinstance(event.get("project_id"), str)
                or not isinstance(event.get("threshold"), (int, float))
                or not isinstance(event.get("status"), str)
                or event.get("status") not in {"PENDING", "DELIVERED"}
            ):
                raise ValueError("invalid budget alert event")
        self.data = loaded

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
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
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

    def record(
        self, *, project_id: str, spend: float, budget: float, request_id: str
    ) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 200:
            return {"status": "INVALID_PROJECT_ID", "alerts": []}
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return {"status": "INVALID_REQUEST_ID", "alerts": []}
        spend_value = finite_number(spend, minimum=0)
        budget_value = finite_number(budget, minimum=0)
        if spend_value is None or budget_value is None or budget_value <= 0:
            return {"status": "INVALID_BUDGET_USAGE", "alerts": []}
        ratio = spend_value / budget_value
        if not math.isfinite(ratio):
            ratio = 1e12
        now = self.clock().astimezone(timezone.utc).isoformat()
        alerts = []
        with self._transaction():
            for threshold in self.thresholds:
                if ratio < threshold:
                    continue
                identity = f"{project_id}\0{budget_value:.12g}\0{threshold:.12g}"
                event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                event = self.data["events"].get(event_id)
                if event is None:
                    event = {
                        "event_id": event_id,
                        "project_id": project_id,
                        "threshold": threshold,
                        "budget": budget_value,
                        "spend": spend_value,
                        "ratio": ratio,
                        "request_id": request_id,
                        "status": "PENDING",
                        "created_at": now,
                    }
                    self.data["events"][event_id] = event
                else:
                    event["spend"] = max(float(event.get("spend", 0.0)), spend_value)
                    event["ratio"] = max(float(event.get("ratio", 0.0)), ratio)
                    event["request_id"] = request_id
                alerts.append(dict(event))
            if alerts:
                self._save()
        return {
            "status": "BUDGET_ALERTS_QUEUED" if alerts else "NO_BUDGET_ALERT",
            "project_id": project_id,
            "spend": spend_value,
            "budget": budget_value,
            "ratio": ratio,
            "alerts": alerts,
        }

    def pending_alerts(self, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            return {"status": "INVALID_LIMIT", "alerts": []}
        with self._transaction():
            events = [
                dict(event) for event in self.data["events"].values()
                if isinstance(event, dict) and event.get("status") == "PENDING"
            ]
            events.sort(key=lambda item: (float(item.get("threshold", 0)), item.get("created_at", "")))
            return {"status": "PENDING_ALERTS", "alerts": events[:limit]}

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        if not isinstance(event_id, str) or not event_id.strip():
            return {"status": "INVALID_EVENT_ID", "saved": False}
        with self._transaction():
            event = self.data["events"].get(event_id)
            if not isinstance(event, dict):
                return {"status": "ALERT_NOT_FOUND", "saved": False}
            if event.get("status") == "DELIVERED":
                return {"status": "ALREADY_DELIVERED", "saved": True}
            event["status"] = "DELIVERED"
            event["delivered_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": "ALERT_DELIVERED", "saved": True}
