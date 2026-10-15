"""Persistent, deduplicated project-budget threshold alerts.

Notifier callbacks should enqueue an event idempotently by event_id and return quickly.
This module never changes budgets, purchases credits, or authorizes paid usage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class BudgetAlertManager:
    def __init__(
        self,
        cost_policy: Any,
        ledger_path: str | os.PathLike[str],
        *,
        thresholds: tuple[int, ...] = (50, 75, 90, 100),
        notifier: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        if not callable(getattr(cost_policy, "budget_snapshot", None)):
            raise ValueError("cost_policy must provide budget_snapshot")
        if not isinstance(ledger_path, (str, os.PathLike)) or not str(ledger_path).strip():
            raise ValueError("ledger_path is required")
        if (
            not isinstance(thresholds, tuple) or not thresholds
            or any(not isinstance(x, int) or isinstance(x, bool) or x < 1 or x > 100 for x in thresholds)
            or tuple(sorted(set(thresholds))) != thresholds
        ):
            raise ValueError("thresholds must be unique ascending integer percentages from 1 to 100")
        if notifier is not None and not callable(notifier):
            raise ValueError("notifier must be callable")
        self.cost_policy = cost_policy
        self.path = Path(ledger_path)
        self.thresholds = thresholds
        self.notifier = notifier
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "events": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.path.exists():
            self._data = {"version": 1, "events": {}}
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid budget alert ledger") from exc
        if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("events"), dict):
            raise ValueError("Invalid budget alert ledger schema")
        for event_id, event in loaded["events"].items():
            if (
                not isinstance(event_id, str) or not event_id
                or not isinstance(event, dict)
                or event.get("event_id") != event_id
                or not isinstance(event.get("project_id"), str) or not event["project_id"].strip()
                or not isinstance(event.get("threshold_pct"), int)
                or isinstance(event.get("threshold_pct"), bool)
                or event["threshold_pct"] not in self.thresholds
                or not isinstance(event.get("budget"), (int, float))
                or isinstance(event.get("budget"), bool)
                or not math.isfinite(event["budget"]) or event["budget"] < 0
                or event.get("delivery_status") not in {"PENDING", "DELIVERED", "NOT_CONFIGURED"}
                or not isinstance(event.get("attempts"), int)
                or isinstance(event.get("attempts"), bool) or event["attempts"] < 0
                or any(
                    not isinstance(event.get(field), (int, float))
                    or isinstance(event.get(field), bool)
                    or not math.isfinite(event[field]) or event[field] < 0
                    for field in ("spent", "reserved", "usage_pct")
                )
                or event_id != self._event_id(
                    event.get("project_id", ""), float(event.get("budget", 0.0)),
                    event.get("threshold_pct", 0),
                )
                or (
                    "last_error_type" in event
                    and (
                        not isinstance(event["last_error_type"], str)
                        or not event["last_error_type"]
                        or len(event["last_error_type"]) > 100
                    )
                )
            ):
                raise ValueError("Invalid budget alert event")
        self._data = loaded

    def _save(self) -> None:
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

    @staticmethod
    def _crossed_thresholds(usage_pct: float, thresholds: tuple[int, ...]) -> tuple[int, ...]:
        """Return configured alert thresholds reached by a validated usage percentage."""
        return tuple(threshold for threshold in thresholds if usage_pct >= threshold)

    @staticmethod
    def _event_id(project_id: str, budget: float, threshold: int) -> str:
        material = f"{project_id}\0{budget:.12g}\0{threshold}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def evaluate_project(self, project_id: str) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 200:
            return {"status": "INVALID_PROJECT", "alerts": [], "delivered": 0}
        try:
            snapshot = self.cost_policy.budget_snapshot(project_id)
        except Exception as exc:
            return {"status": "BUDGET_SNAPSHOT_FAILED", "alerts": [], "error_type": type(exc).__name__, "delivered": 0}
        if not isinstance(snapshot, dict) or snapshot.get("status") != "OK":
            return {
                "status": snapshot.get("status", "INVALID_BUDGET_SNAPSHOT") if isinstance(snapshot, dict) else "INVALID_BUDGET_SNAPSHOT",
                "alerts": [], "delivered": 0,
            }
        budget = snapshot["budget"]
        spent = snapshot["spent"]
        reserved = snapshot["reserved"]
        usage = spent + reserved
        percent = 100.0 if budget == 0 and usage > 0 else (0.0 if budget == 0 else usage * 100.0 / budget)
        new_events: list[dict[str, Any]] = []
        delivered = 0
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._lock, interprocess_file_lock(lock_path):
            self._reload()
            for threshold in self._crossed_thresholds(percent, self.thresholds):
                event_id = self._event_id(project_id, float(budget), threshold)
                if event_id in self._data["events"]:
                    continue
                event = {
                    "event_id": event_id,
                    "project_id": project_id,
                    "threshold_pct": threshold,
                    "budget": float(budget),
                    "spent": float(spent),
                    "reserved": float(reserved),
                    "usage_pct": round(percent, 4),
                    "delivery_status": "PENDING" if self.notifier is not None else "NOT_CONFIGURED",
                    "attempts": 0,
                }
                self._data["events"][event_id] = event
                new_events.append(dict(event))
            self._save()
            pending = [
                dict(event) for event in self._data["events"].values()
                if event["project_id"] == project_id and event["delivery_status"] != "DELIVERED"
            ]
            if self.notifier is not None:
                for event in pending:
                    try:
                        self.notifier(dict(event))
                    except Exception as exc:
                        current = self._data["events"][event["event_id"]]
                        current["delivery_status"] = "PENDING"
                        current["attempts"] += 1
                        current["last_error_type"] = type(exc).__name__
                    else:
                        current = self._data["events"][event["event_id"]]
                        current["delivery_status"] = "DELIVERED"
                        current["attempts"] += 1
                        current.pop("last_error_type", None)
                        delivered += 1
                self._save()
        alerts = [
            dict(event) for event in self._data["events"].values()
            if event["project_id"] == project_id
        ]
        alerts.sort(key=lambda event: event["threshold_pct"])
        return {
            "status": "ALERTS_EVALUATED",
            "project_id": project_id,
            "budget": float(budget),
            "spent": float(spent),
            "reserved": float(reserved),
            "usage_pct": round(percent, 4),
            "alerts": alerts,
            "new_alert_count": len(new_events),
            "delivered_count": delivered,
        }

    def list_events(self, project_id: str | None = None) -> dict[str, Any]:
        if project_id is not None and (
            not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 200
        ):
            return {"status": "INVALID_PROJECT", "events": []}
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._lock, interprocess_file_lock(lock_path):
            self._reload()
            events = [
                dict(event) for event in self._data["events"].values()
                if project_id is None or event["project_id"] == project_id
            ]
        events.sort(key=lambda event: (event["project_id"], event["threshold_pct"]))
        return {"status": "OK", "events": events}
