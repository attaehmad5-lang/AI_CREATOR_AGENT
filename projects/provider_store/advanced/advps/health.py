"""Persistent provider health state and scheduled-probe leases; probes are injected by the host."""
from __future__ import annotations

import math
import re
import time
from typing import Callable, Iterable, Optional

from .errors import NotFoundError, ValidationError
from .storage import AtomicJsonStore


def _empty() -> dict:
    return {"schema": 1, "providers": {}}


def _number(value, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{name} must be finite")
    number = float(value)
    if number < 0 or (positive and number <= 0):
        raise ValidationError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return number


def _new_record(now: float) -> dict:
    return {
        "status": "UNKNOWN", "consecutive_failures": 0, "last_success_at": None,
        "last_failure_at": None, "next_probe_at": now, "latency_ewma_ms": None,
        "last_error_code": None, "probe_lease_owner": None, "probe_lease_expires_at": None,
    }


class HealthRegistry:
    """Stores sanitized health outcomes and due-probe leases; it never performs network probes."""

    def __init__(
        self,
        store: AtomicJsonStore,
        *,
        clock: Callable[[], float] = time.time,
        probe_interval: float = 60.0,
        failure_threshold: int = 3,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        ewma_alpha: float = 0.3,
    ):
        self.store = store
        self.store.default = _empty
        self.clock = clock
        self.probe_interval = _number(probe_interval, "probe_interval", positive=True)
        self.base_backoff = _number(base_backoff, "base_backoff", positive=True)
        self.max_backoff = _number(max_backoff, "max_backoff", positive=True)
        if self.max_backoff < self.base_backoff:
            raise ValidationError("max_backoff must be >= base_backoff")
        if isinstance(failure_threshold, bool) or not isinstance(failure_threshold, int) or failure_threshold < 1:
            raise ValidationError("failure_threshold must be a positive integer")
        if isinstance(ewma_alpha, bool) or not isinstance(ewma_alpha, (int, float)) or not 0 < ewma_alpha <= 1:
            raise ValidationError("ewma_alpha must be in (0, 1]")
        self.failure_threshold = failure_threshold
        self.ewma_alpha = float(ewma_alpha)

    @staticmethod
    def _provider_id(provider_id: str) -> str:
        if not isinstance(provider_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", provider_id):
            raise ValidationError("invalid provider_id")
        return provider_id

    def get(self, provider_id: str) -> dict:
        provider_id = self._provider_id(provider_id)
        record = self.store.read()["providers"].get(provider_id)
        if record is None:
            raise NotFoundError(f"unknown provider health: {provider_id}")
        return record

    def list(self) -> dict:
        return self.store.read()["providers"]

    def due(self, provider_ids: Iterable[str]) -> list[str]:
        now = self.clock()
        state = self.store.read()["providers"]
        out = []
        for raw_id in provider_ids:
            provider_id = self._provider_id(raw_id)
            record = state.get(provider_id)
            if record is None or record["next_probe_at"] <= now:
                if record is None or record["probe_lease_expires_at"] is None or record["probe_lease_expires_at"] <= now:
                    out.append(provider_id)
        return out

    def claim_due(self, provider_ids: Iterable[str], worker_id: str, lease_seconds: float = 30.0, limit: Optional[int] = None) -> list[str]:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValidationError("worker_id must be non-empty")
        lease_seconds = _number(lease_seconds, "lease_seconds", positive=True)
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValidationError("limit must be a positive integer or None")
        ids = sorted(set(self._provider_id(pid) for pid in provider_ids))
        claimed: list[str] = []

        def op(state):
            now = self.clock()
            for provider_id in ids:
                if limit is not None and len(claimed) >= limit:
                    break
                record = state["providers"].setdefault(provider_id, _new_record(now))
                lease = record["probe_lease_expires_at"]
                if record["next_probe_at"] > now or (lease is not None and lease > now):
                    continue
                record["probe_lease_owner"] = worker_id
                record["probe_lease_expires_at"] = now + lease_seconds
                claimed.append(provider_id)
        self.store.update(op)
        return claimed

    def _success(self, record: dict, now: float, latency_ms: Optional[float]) -> None:
        record["status"] = "HEALTHY"
        record["consecutive_failures"] = 0
        record["last_success_at"] = now
        record["next_probe_at"] = now + self.probe_interval
        record["last_error_code"] = None
        record["probe_lease_owner"] = None
        record["probe_lease_expires_at"] = None
        if latency_ms is not None:
            latency = _number(latency_ms, "latency_ms")
            old = record["latency_ewma_ms"]
            record["latency_ewma_ms"] = latency if old is None else self.ewma_alpha * latency + (1 - self.ewma_alpha) * old

    def _failure(self, record: dict, now: float, error_code: str, retry_after: Optional[float]) -> None:
        if not isinstance(error_code, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", error_code):
            raise ValidationError("error_code must be a short identifier")
        failures = record["consecutive_failures"] + 1
        backoff = min(self.max_backoff, self.base_backoff * (2 ** min(failures - 1, 30)))
        if retry_after is not None:
            backoff = max(backoff, _number(retry_after, "retry_after"))
        record["status"] = "UNHEALTHY" if failures >= self.failure_threshold else "DEGRADED"
        record["consecutive_failures"] = failures
        record["last_failure_at"] = now
        record["next_probe_at"] = now + backoff
        record["last_error_code"] = error_code
        record["probe_lease_owner"] = None
        record["probe_lease_expires_at"] = None

    def record_success(self, provider_id: str, latency_ms: Optional[float] = None) -> dict:
        provider_id = self._provider_id(provider_id)
        result = {}
        def op(state):
            now = self.clock()
            record = state["providers"].setdefault(provider_id, _new_record(now))
            self._success(record, now, latency_ms)
            result.update(record)
        self.store.update(op)
        return result

    def record_failure(self, provider_id: str, error_code: str, retry_after: Optional[float] = None) -> dict:
        provider_id = self._provider_id(provider_id)
        result = {}
        def op(state):
            now = self.clock()
            record = state["providers"].setdefault(provider_id, _new_record(now))
            self._failure(record, now, error_code, retry_after)
            result.update(record)
        self.store.update(op)
        return result

    def finish_probe(
        self, provider_id: str, worker_id: str, *, success: bool,
        latency_ms: Optional[float] = None, error_code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> dict:
        provider_id = self._provider_id(provider_id)
        if not isinstance(success, bool):
            raise ValidationError("success must be boolean")
        result = {}
        def op(state):
            record = state["providers"].get(provider_id)
            if record is None:
                raise NotFoundError(f"unknown provider health: {provider_id}")
            now = self.clock()
            if record["probe_lease_owner"] != worker_id or record["probe_lease_expires_at"] is None:
                raise ValidationError("worker does not own a probe lease")
            if record["probe_lease_expires_at"] <= now:
                raise ValidationError("probe lease expired")
            if success:
                self._success(record, now, latency_ms)
            else:
                self._failure(record, now, error_code or "probe_failed", retry_after)
            result.update(record)
        self.store.update(op)
        return result
