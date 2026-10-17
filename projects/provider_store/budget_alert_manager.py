"""Persistent, idempotent budget-threshold alert events.

This module queues alert events for an application-owned notification delivery layer.
It never sends messages or triggers purchases on its own.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
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
        delivery_lease_seconds: int = 60,
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
        if (
            isinstance(delivery_lease_seconds, bool)
            or not isinstance(delivery_lease_seconds, int)
            or not 1 <= delivery_lease_seconds <= 3600
        ):
            raise ValueError("delivery_lease_seconds must be within 1..3600")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.thresholds = tuple(normalized)
        self.delivery_lease_seconds = delivery_lease_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "events": {}}
        self._reload()

    @staticmethod
    def _valid_event_timestamp(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError, OverflowError):
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

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
                or not event.get("project_id", "").strip()
                or isinstance(event.get("threshold"), bool)
                or finite_number(event.get("threshold"), minimum=0) is None
                or not 0 < float(event.get("threshold", 0)) <= 1
                or isinstance(event.get("budget"), bool)
                or finite_number(event.get("budget"), minimum=0) is None
                or float(event.get("budget", 0)) <= 0
                or isinstance(event.get("spend"), bool)
                or finite_number(event.get("spend"), minimum=0) is None
                or isinstance(event.get("ratio"), bool)
                or finite_number(event.get("ratio"), minimum=0) is None
                or not self._valid_event_timestamp(event.get("created_at"))
                or (
                    "delivery_attempts" in event
                    and (
                        isinstance(event.get("delivery_attempts"), bool)
                        or not isinstance(event.get("delivery_attempts"), int)
                        or event["delivery_attempts"] < 0
                    )
                )
                or (
                    event.get("budget_period_id") is not None
                    and (
                        not isinstance(event.get("budget_period_id"), str)
                        or not event["budget_period_id"].strip()
                    )
                )
                or not isinstance(event.get("status"), str)
                or event.get("status") not in {"PENDING", "DELIVERING", "DELIVERED"}
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
        self, *, project_id: str, spend: float, budget: float, request_id: str,
        budget_period_id: str = "default",
    ) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 200:
            return {"status": "INVALID_PROJECT_ID", "alerts": []}
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return {"status": "INVALID_REQUEST_ID", "alerts": []}
        if (
            not isinstance(budget_period_id, str) or not budget_period_id.strip()
            or len(budget_period_id) > 200
            or any(ord(char) < 32 or ord(char) == 127 for char in budget_period_id)
        ):
            return {"status": "INVALID_BUDGET_PERIOD_ID", "alerts": []}
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
                identity = f"{project_id}\0{budget_period_id}\0{budget_value:.12g}\0{threshold:.12g}"
                event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                event = self.data["events"].get(event_id)
                if event is None:
                    event = {
                        "event_id": event_id,
                        "project_id": project_id,
                        "budget_period_id": budget_period_id,
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

    def _claim_pending_alerts(self, *, limit: int) -> list[dict[str, Any]]:
        now = self.clock().astimezone(timezone.utc)
        now_iso = now.isoformat()
        claimed: list[dict[str, Any]] = []
        with self._transaction():
            events = sorted(
                self.data["events"].values(),
                key=lambda item: (float(item.get("threshold", 0)), item.get("created_at", "")),
            )
            for event in events:
                if not isinstance(event, dict):
                    continue
                status = event.get("status")
                reclaimable = status == "PENDING"
                if status == "DELIVERING":
                    try:
                        lease = datetime.fromisoformat(event.get("delivery_lease_until", ""))
                        reclaimable = lease.tzinfo is None or lease.astimezone(timezone.utc) <= now
                    except (ValueError, TypeError, OverflowError):
                        reclaimable = True
                if not reclaimable:
                    continue
                event["status"] = "DELIVERING"
                event["delivery_token"] = secrets.token_urlsafe(24)
                event["delivery_lease_until"] = (
                    now + __import__("datetime").timedelta(seconds=self.delivery_lease_seconds)
                ).isoformat()
                event["delivery_attempts"] = min(
                    int(event.get("delivery_attempts", 0)) + 1, 2**31 - 1
                )
                event["last_delivery_attempt_at"] = now_iso
                claimed.append(dict(event))
                if len(claimed) >= limit:
                    break
            if claimed:
                self._save()
        return claimed

    def _finish_delivery(
        self, event_id: str, delivery_token: str, *, delivered: bool,
        failure_code: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction():
            event = self.data["events"].get(event_id)
            if (
                not isinstance(event, dict)
                or event.get("status") != "DELIVERING"
                or not secrets.compare_digest(str(event.get("delivery_token", "")), delivery_token)
            ):
                return {"status": "DELIVERY_LEASE_LOST", "saved": False}
            event.pop("delivery_token", None)
            event.pop("delivery_lease_until", None)
            if delivered:
                event["status"] = "DELIVERED"
                event["delivered_at"] = self.clock().astimezone(timezone.utc).isoformat()
            else:
                event["status"] = "PENDING"
                event["last_delivery_error_code"] = (
                    failure_code if failure_code in {
                        "DELIVERY_CALLBACK_REJECTED", "DELIVERY_CALLBACK_FAILED"
                    } else "DELIVERY_CALLBACK_FAILED"
                )
            self._save()
            return {
                "status": "ALERT_DELIVERED" if delivered else event["last_delivery_error_code"],
                "saved": True,
            }

    def dispatch_pending(self, sender: Any, *, limit: int = 20) -> dict[str, Any]:
        """Deliver claimed alerts with leases so concurrent dispatchers do not send the same event."""
        if not callable(sender):
            return {"status": "INVALID_DELIVERY_CALLBACK", "attempted": 0, "delivered": 0, "failed": 0}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return {"status": "INVALID_LIMIT", "attempted": 0, "delivered": 0, "failed": 0}
        claimed = self._claim_pending_alerts(limit=limit)
        outcomes = []
        for alert in claimed:
            token = alert.get("delivery_token")
            try:
                response = sender(dict(alert))
                delivered = response is True or (
                    isinstance(response, dict) and response.get("delivered") is True
                )
                failure_code = None if delivered else "DELIVERY_CALLBACK_REJECTED"
            except Exception:
                delivered = False
                failure_code = "DELIVERY_CALLBACK_FAILED"
            result = self._finish_delivery(
                alert["event_id"], str(token), delivered=delivered,
                failure_code=failure_code,
            )
            outcomes.append({
                "event_id": alert["event_id"],
                "status": result.get("status", "DELIVERY_STATUS_UNKNOWN"),
            })
        delivered_count = sum(item["status"] in {"ALERT_DELIVERED", "ALREADY_DELIVERED"} for item in outcomes)
        failed_count = len(outcomes) - delivered_count
        return {
            "status": "DELIVERY_ATTEMPTS_COMPLETED",
            "attempted": len(outcomes),
            "delivered": delivered_count,
            "failed": failed_count,
            "results": outcomes,
        }

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
            event.pop("delivery_token", None)
            event.pop("delivery_lease_until", None)
            event["delivered_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": "ALERT_DELIVERED", "saved": True}
