"""Persistent, idempotent budget alerts with leased, bounded delivery retries.

The manager queues notification events only. The application supplies the sender;
this module never sends messages, buys credits, or changes provider subscriptions.
Delivery is at-least-once across process crashes. Senders should use event_id as the
external notification idempotency key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


_ERROR_CODES = {"DELIVERY_CALLBACK_FAILED", "DELIVERY_CALLBACK_REJECTED"}
_STATUSES = {"PENDING", "DELIVERED", "FAILED_PERMANENT"}


class BudgetAlertManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        thresholds: tuple[float, ...] = (0.5, 0.8, 1.0),
        clock: Any = None,
        max_delivery_attempts: int = 5,
        retry_base_seconds: float = 30.0,
        retry_max_seconds: float = 900.0,
        dispatch_lease_seconds: int = 120,
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
        if (
            isinstance(max_delivery_attempts, bool)
            or not isinstance(max_delivery_attempts, int)
            or not 1 <= max_delivery_attempts <= 20
        ):
            raise ValueError("max_delivery_attempts must be within 1..20")
        base = finite_number(retry_base_seconds, minimum=0)
        maximum = finite_number(retry_max_seconds, minimum=0)
        if base is None or base <= 0 or maximum is None or maximum < base or maximum > 86400:
            raise ValueError("retry delays must satisfy 0 < base <= max <= 86400")
        if (
            isinstance(dispatch_lease_seconds, bool)
            or not isinstance(dispatch_lease_seconds, int)
            or not 5 <= dispatch_lease_seconds <= 3600
        ):
            raise ValueError("dispatch_lease_seconds must be within 5..3600")

        self.state_path = Path(state_path)
        self.thresholds = tuple(normalized)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_delivery_attempts = max_delivery_attempts
        self.retry_base_seconds = float(base)
        self.retry_max_seconds = float(maximum)
        self.dispatch_lease_seconds = dispatch_lease_seconds
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "events": {}}
        self._reload()

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

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
    def _remove_lease(event: dict[str, Any]) -> None:
        event.pop("delivery_lease_token", None)
        event.pop("delivery_lease_owner", None)
        event.pop("delivery_lease_until", None)

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "events": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid budget alert state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version", "1.0") != "1.0"
            or not isinstance(loaded.get("events"), dict)
        ):
            raise ValueError("invalid budget alert state schema")
        for event_id, event in loaded["events"].items():
            if (
                not isinstance(event_id, str)
                or not re.fullmatch(r"[0-9a-f]{64}", event_id)
                or not isinstance(event, dict)
                or not isinstance(event.get("project_id"), str)
                or not event["project_id"].strip()
                or finite_number(event.get("threshold"), minimum=0) is None
                or not 0 < float(event["threshold"]) <= 1
                or finite_number(event.get("budget"), minimum=0) is None
                or float(event["budget"]) <= 0
                or finite_number(event.get("spend"), minimum=0) is None
                or finite_number(event.get("ratio"), minimum=0) is None
                or not isinstance(event.get("request_id"), str)
                or not event["request_id"].strip()
                or event.get("status") not in _STATUSES
                or self._parse_timestamp(event.get("created_at")) is None
            ):
                raise ValueError("invalid budget alert event")
            period_id = event.get("budget_period_id")
            if period_id is not None and (
                not isinstance(period_id, str) or not period_id or len(period_id) > 200
            ):
                raise ValueError("invalid budget alert period")
            attempts = event.get("delivery_attempts", 0)
            if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 2**31 - 1:
                raise ValueError("invalid budget alert delivery attempts")
            if "next_delivery_at" in event and self._parse_timestamp(event.get("next_delivery_at")) is None:
                raise ValueError("invalid budget alert retry timestamp")
            if "last_delivery_attempt_at" in event and self._parse_timestamp(event.get("last_delivery_attempt_at")) is None:
                raise ValueError("invalid budget alert attempt timestamp")
            if "last_delivery_error_code" in event and event.get("last_delivery_error_code") not in _ERROR_CODES:
                raise ValueError("invalid budget alert delivery error code")
            if "delivered_at" in event and self._parse_timestamp(event.get("delivered_at")) is None:
                raise ValueError("invalid budget alert delivery timestamp")
            if "failed_at" in event and self._parse_timestamp(event.get("failed_at")) is None:
                raise ValueError("invalid budget alert failure timestamp")
            lease_token = event.get("delivery_lease_token")
            lease_owner = event.get("delivery_lease_owner")
            lease_until = event.get("delivery_lease_until")
            if any(value is not None for value in (lease_token, lease_owner, lease_until)):
                if (
                    not isinstance(lease_token, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", lease_token)
                    or not isinstance(lease_owner, str)
                    or not lease_owner
                    or len(lease_owner) > 128
                    or self._parse_timestamp(lease_until) is None
                    or event.get("status") != "PENDING"
                ):
                    raise ValueError("invalid budget alert dispatch lease")
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
        self,
        *,
        project_id: str,
        spend: float,
        budget: float,
        request_id: str,
        budget_period_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 200:
            return {"status": "INVALID_PROJECT_ID", "alerts": []}
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return {"status": "INVALID_REQUEST_ID", "alerts": []}
        if budget_period_id is not None and (
            not isinstance(budget_period_id, str)
            or not budget_period_id.strip()
            or len(budget_period_id) > 200
        ):
            return {"status": "INVALID_BUDGET_PERIOD_ID", "alerts": []}
        spend_value = finite_number(spend, minimum=0)
        budget_value = finite_number(budget, minimum=0)
        if spend_value is None or budget_value is None or budget_value <= 0:
            return {"status": "INVALID_BUDGET_USAGE", "alerts": []}
        ratio = spend_value / budget_value
        if not math.isfinite(ratio):
            ratio = 1e12
        try:
            now = self._now()
        except ValueError:
            return {"status": "INVALID_CLOCK", "alerts": []}
        timestamp = now.isoformat()
        alerts = []
        with self._transaction():
            for threshold in self.thresholds:
                if ratio < threshold:
                    continue
                # Preserve the legacy event ID when no period is supplied, while
                # allowing recurring budgets to emit one alert per explicit cycle.
                identity = f"{project_id}\0{budget_value:.12g}\0{threshold:.12g}"
                if budget_period_id is not None:
                    identity += f"\0{budget_period_id}"
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
                        "created_at": timestamp,
                    }
                    if budget_period_id is not None:
                        event["budget_period_id"] = budget_period_id
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

    def failed_alerts(self, *, limit: int = 100) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            return {"status": "INVALID_LIMIT", "alerts": []}
        with self._transaction():
            events = [
                dict(event) for event in self.data["events"].values()
                if isinstance(event, dict) and event.get("status") == "FAILED_PERMANENT"
            ]
            events.sort(key=lambda item: (item.get("failed_at", ""), item.get("event_id", "")))
            return {"status": "FAILED_ALERTS", "alerts": events[:limit]}

    def _settle_delivery(
        self, *, event_id: str, lease_token: str, delivered: bool, failure_code: str | None
    ) -> dict[str, Any]:
        try:
            now = self._now()
        except ValueError:
            return {"status": "INVALID_CLOCK", "saved": False}
        with self._transaction():
            event = self.data["events"].get(event_id)
            if (
                not isinstance(event, dict)
                or event.get("status") != "PENDING"
                or event.get("delivery_lease_token") != lease_token
            ):
                return {"status": "DELIVERY_CLAIM_LOST", "saved": False}
            attempts = min(int(event.get("delivery_attempts", 0)) + 1, 2**31 - 1)
            event["delivery_attempts"] = attempts
            event["last_delivery_attempt_at"] = now.isoformat()
            self._remove_lease(event)
            if delivered:
                event["status"] = "DELIVERED"
                event["delivered_at"] = now.isoformat()
                event.pop("next_delivery_at", None)
                event.pop("last_delivery_error_code", None)
                self._save()
                return {"status": "ALERT_DELIVERED", "saved": True}
            if failure_code not in _ERROR_CODES:
                failure_code = "DELIVERY_CALLBACK_FAILED"
            event["last_delivery_error_code"] = failure_code
            if attempts >= self.max_delivery_attempts:
                event["status"] = "FAILED_PERMANENT"
                event["failed_at"] = now.isoformat()
                event.pop("next_delivery_at", None)
                self._save()
                return {"status": "DELIVERY_EXHAUSTED", "saved": True}
            delay = min(
                self.retry_base_seconds * (2 ** max(0, attempts - 1)),
                self.retry_max_seconds,
            )
            event["next_delivery_at"] = (now + timedelta(seconds=delay)).isoformat()
            self._save()
            return {"status": "DELIVERY_RETRY_SCHEDULED", "saved": True, "retry_after_seconds": delay}

    def dispatch_pending(
        self,
        sender: Any,
        *,
        limit: int = 20,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Claim due alerts, send outside state locks, and persist bounded outcomes.

        A per-state dispatch lock prevents concurrent processes from sending the same
        event. Persisted leases permit recovery if a worker exits during delivery.
        """
        if not callable(sender):
            return {"status": "INVALID_DELIVERY_CALLBACK", "attempted": 0, "delivered": 0, "failed": 0}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return {"status": "INVALID_LIMIT", "attempted": 0, "delivered": 0, "failed": 0}
        if worker_id is None:
            worker_id = uuid.uuid4().hex
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", worker_id):
            return {"status": "INVALID_WORKER_ID", "attempted": 0, "delivered": 0, "failed": 0}
        try:
            now = self._now()
        except ValueError:
            return {"status": "INVALID_CLOCK", "attempted": 0, "delivered": 0, "failed": 0}

        claimed: list[tuple[dict[str, Any], str]] = []
        deferred = 0
        recovered = 0
        with self._transaction():
            ordered = sorted(
                self.data["events"].values(),
                key=lambda item: (float(item.get("threshold", 0)), item.get("created_at", "")),
            )
            changed = False
            for event in ordered:
                if not isinstance(event, dict) or event.get("status") != "PENDING":
                    continue
                retry_at = self._parse_timestamp(event.get("next_delivery_at"))
                if retry_at is not None and retry_at > now:
                    deferred += 1
                    continue
                lease_until = self._parse_timestamp(event.get("delivery_lease_until"))
                if lease_until is not None and lease_until > now:
                    deferred += 1
                    continue
                if event.get("delivery_lease_token") is not None:
                    self._remove_lease(event)
                    recovered += 1
                    changed = True
                attempts = int(event.get("delivery_attempts", 0))
                if attempts >= self.max_delivery_attempts:
                    event["status"] = "FAILED_PERMANENT"
                    event["failed_at"] = now.isoformat()
                    event.pop("next_delivery_at", None)
                    changed = True
                    continue
                token = uuid.uuid4().hex
                event["delivery_lease_token"] = token
                event["delivery_lease_owner"] = worker_id
                event["delivery_lease_until"] = (
                    now + timedelta(seconds=self.dispatch_lease_seconds)
                ).isoformat()
                claimed.append((dict(event), token))
                changed = True
                if len(claimed) >= limit:
                    break
            if changed:
                self._save()

        outcomes = []
        for alert, token in claimed:
            try:
                response = sender(dict(alert))
                delivered = response is True or (
                    isinstance(response, dict) and response.get("delivered") is True
                )
                failure_code = None if delivered else "DELIVERY_CALLBACK_REJECTED"
            except Exception:
                delivered = False
                failure_code = "DELIVERY_CALLBACK_FAILED"
            result = self._settle_delivery(
                event_id=alert["event_id"],
                lease_token=token,
                delivered=delivered,
                failure_code=failure_code,
            )
            outcomes.append({"event_id": alert["event_id"], "status": result.get("status", "DELIVERY_STATUS_UNKNOWN")})

        delivered_count = sum(item["status"] in {"ALERT_DELIVERED", "ALREADY_DELIVERED"} for item in outcomes)
        failed_count = sum(
            item["status"] in {"DELIVERY_RETRY_SCHEDULED", "DELIVERY_EXHAUSTED", "DELIVERY_CLAIM_LOST"}
            for item in outcomes
        )
        return {
            "status": "DELIVERY_ATTEMPTS_COMPLETED",
            "attempted": len(outcomes),
            "delivered": delivered_count,
            "failed": failed_count,
            "deferred": deferred,
            "recovered": recovered,
            "results": outcomes,
        }

    def requeue_failed(self, event_id: str) -> dict[str, Any]:
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
            return {"status": "INVALID_EVENT_ID", "saved": False}
        try:
            now = self._now()
        except ValueError:
            return {"status": "INVALID_CLOCK", "saved": False}
        with self._transaction():
            event = self.data["events"].get(event_id)
            if not isinstance(event, dict):
                return {"status": "ALERT_NOT_FOUND", "saved": False}
            if event.get("status") != "FAILED_PERMANENT":
                return {"status": "ALERT_NOT_FAILED_PERMANENT", "saved": False}
            event["status"] = "PENDING"
            event["delivery_attempts"] = 0
            event["manual_requeue_count"] = min(int(event.get("manual_requeue_count", 0)) + 1, 2**31 - 1)
            event.pop("failed_at", None)
            event.pop("last_delivery_error_code", None)
            event.pop("last_delivery_attempt_at", None)
            event.pop("next_delivery_at", None)
            self._remove_lease(event)
            event["requeued_at"] = now.isoformat()
            self._save()
            return {"status": "ALERT_REQUEUED", "saved": True}

    def mark_delivered(self, event_id: str) -> dict[str, Any]:
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{64}", event_id):
            return {"status": "INVALID_EVENT_ID", "saved": False}
        try:
            now = self._now()
        except ValueError:
            return {"status": "INVALID_CLOCK", "saved": False}
        with self._transaction():
            event = self.data["events"].get(event_id)
            if not isinstance(event, dict):
                return {"status": "ALERT_NOT_FOUND", "saved": False}
            if event.get("status") == "DELIVERED":
                return {"status": "ALREADY_DELIVERED", "saved": True}
            if event.get("status") != "PENDING":
                return {"status": "ALERT_NOT_PENDING", "saved": False}
            event["status"] = "DELIVERED"
            event["delivered_at"] = now.isoformat()
            event.pop("next_delivery_at", None)
            event.pop("last_delivery_error_code", None)
            self._remove_lease(event)
            self._save()
            return {"status": "ALERT_DELIVERED", "saved": True}
