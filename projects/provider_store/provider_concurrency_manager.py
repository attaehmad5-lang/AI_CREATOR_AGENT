"""Cross-process provider concurrency limits backed by renewable execution leases."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


class ProviderConcurrencyManager:
    """Enforce per-provider active execution limits across cooperating processes."""

    def __init__(
        self, path: str | os.PathLike[str], *,
        provider_limits: dict[str, int] | None = None,
        default_limit: int = 2,
        lease_seconds: int = 120,
        heartbeat_interval: float | None = None,
        max_leases: int = 100_000,
        clock: Any = time.time,
    ) -> None:
        if isinstance(default_limit, bool) or not isinstance(default_limit, int) or not 1 <= default_limit <= 10_000:
            raise ValueError("default_limit must be between 1 and 10000")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 3 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 3 and 86400")
        if isinstance(max_leases, bool) or not isinstance(max_leases, int) or not 1 <= max_leases <= 1_000_000:
            raise ValueError("max_leases must be between 1 and 1000000")
        if heartbeat_interval is None:
            heartbeat_interval = max(0.5, lease_seconds / 3)
        if (
            isinstance(heartbeat_interval, bool) or not isinstance(heartbeat_interval, (int, float))
            or not math.isfinite(float(heartbeat_interval))
            or not 0.1 <= heartbeat_interval < lease_seconds
        ):
            raise ValueError("heartbeat_interval must be positive and shorter than the lease")
        if not callable(clock):
            raise ValueError("clock must be callable")
        limits = {} if provider_limits is None else provider_limits
        if not isinstance(limits, dict):
            raise ValueError("provider_limits must be a mapping")
        clean_limits: dict[str, int] = {}
        for provider_id, limit in limits.items():
            if not self._valid_id(provider_id):
                raise ValueError("provider limit identifier is invalid")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
                raise ValueError("provider limit must be between 1 and 10000")
            clean_limits[provider_id] = limit

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.provider_limits = clean_limits
        self.default_limit = default_limit
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = float(heartbeat_interval)
        self.max_leases = max_leases
        self._clock = clock
        self._lock = threading.RLock()
        if not self.path.exists():
            with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
                if not self.path.exists():
                    self._save({"version": 1, "leases": {}})
        self._load()

    @staticmethod
    def _valid_id(value: Any, *, optional: bool = False) -> bool:
        if value is None and optional:
            return True
        return (
            isinstance(value, str) and 1 <= len(value) <= 200
            and value[0].isalnum()
            and all(char in _ID_CHARS for char in value)
        )

    def _now(self, value: Any = None) -> float:
        current = self._clock() if value is None else value
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError("invalid concurrency clock")
        current = float(current)
        if not math.isfinite(current) or current < 0:
            raise ValueError("invalid concurrency clock")
        return current

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError("provider concurrency state is unavailable or malformed") from None
        if not isinstance(state, dict) or set(state) != {"version", "leases"} or state.get("version") != 1:
            raise ValueError("provider concurrency state schema is invalid")
        leases = state.get("leases")
        if not isinstance(leases, dict) or len(leases) > self.max_leases:
            raise ValueError("provider concurrency lease collection is invalid")
        required = {
            "provider_id", "tenant_id", "project_id", "request_id",
            "worker_id", "lease_token", "acquired_at", "expires_at", "renewed_at",
        }
        for token, lease in leases.items():
            if not isinstance(token, str) or not self._valid_id(token):
                raise ValueError("provider concurrency lease key is invalid")
            if not isinstance(lease, dict) or set(lease) != required:
                raise ValueError("provider concurrency lease schema is invalid")
            for field in ("provider_id", "project_id", "request_id", "worker_id"):
                if not self._valid_id(lease.get(field)):
                    raise ValueError("provider concurrency lease identifier is invalid")
            if not self._valid_id(lease.get("tenant_id"), optional=True):
                raise ValueError("provider concurrency tenant identifier is invalid")
            if lease.get("lease_token") != token:
                raise ValueError("provider concurrency lease token mismatch")
            times = []
            for field in ("acquired_at", "expires_at", "renewed_at"):
                value = lease.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError("provider concurrency lease timestamp is invalid")
                times.append(float(value))
            acquired, expires, renewed = times
            if not acquired <= renewed <= expires:
                raise ValueError("provider concurrency lease timestamps are inconsistent")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            atomic_replace_with_retry(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _purge_expired(self, leases: dict[str, Any], now: float) -> int:
        expired = [
            token for token, lease in leases.items()
            if isinstance(lease, dict) and lease.get("expires_at", 0) <= now
        ]
        for token in expired:
            leases.pop(token, None)
        return len(expired)

    def acquire(
        self, *, provider_id: str, request_id: str, project_id: str,
        tenant_id: str | None = None, worker_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if (
            not self._valid_id(provider_id) or not self._valid_id(request_id)
            or not self._valid_id(project_id) or not self._valid_id(tenant_id, optional=True)
        ):
            return {"acquired": False, "status": "INVALID_CONCURRENCY_IDENTITY"}
        worker = worker_id or uuid.uuid4().hex
        if not self._valid_id(worker):
            return {"acquired": False, "status": "INVALID_WORKER_ID"}
        try:
            current = self._now(now)
        except (TypeError, ValueError, OverflowError):
            return {"acquired": False, "status": "INVALID_CONCURRENCY_TIMESTAMP"}
        token = uuid.uuid4().hex
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"acquired": False, "status": "CONCURRENCY_STATE_INVALID"}
            leases = state["leases"]
            self._purge_expired(leases, current)
            for lease in leases.values():
                if (
                    lease["provider_id"] == provider_id
                    and lease["project_id"] == project_id
                    and lease["tenant_id"] == tenant_id
                    and lease["request_id"] == request_id
                ):
                    return {"acquired": False, "status": "DUPLICATE_CONCURRENCY_REQUEST"}
            limit = self.provider_limits.get(provider_id, self.default_limit)
            active = [
                lease for lease in leases.values()
                if lease["provider_id"] == provider_id
            ]
            if len(active) >= limit:
                earliest = min(lease["expires_at"] for lease in active)
                return {
                    "acquired": False, "status": "PROVIDER_CONCURRENCY_LIMIT_REACHED",
                    "provider_id": provider_id, "active": len(active), "limit": limit,
                    "retry_after_seconds": max(1, int(math.ceil(earliest - current))),
                }
            if len(leases) >= self.max_leases:
                return {"acquired": False, "status": "CONCURRENCY_CAPACITY_REACHED"}
            lease = {
                "provider_id": provider_id, "tenant_id": tenant_id,
                "project_id": project_id, "request_id": request_id,
                "worker_id": worker, "lease_token": token,
                "acquired_at": current, "expires_at": current + self.lease_seconds,
                "renewed_at": current,
            }
            leases[token] = lease
            try:
                self._save(state)
            except OSError:
                return {"acquired": False, "status": "CONCURRENCY_PERSISTENCE_FAILED"}
            return {
                "acquired": True, "status": "PROVIDER_CONCURRENCY_LEASE_ACQUIRED",
                "provider_id": provider_id, "lease_token": token,
                "worker_id": worker, "expires_at": lease["expires_at"],
                "active": len(active) + 1, "limit": limit,
            }

    def renew(self, lease_token: str, *, now: float | None = None) -> dict[str, Any]:
        if not self._valid_id(lease_token):
            return {"renewed": False, "status": "INVALID_CONCURRENCY_LEASE"}
        try:
            current = self._now(now)
        except (TypeError, ValueError, OverflowError):
            return {"renewed": False, "status": "INVALID_CONCURRENCY_TIMESTAMP"}
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"renewed": False, "status": "CONCURRENCY_STATE_INVALID"}
            lease = state["leases"].get(lease_token)
            if not isinstance(lease, dict):
                return {"renewed": False, "status": "CONCURRENCY_LEASE_NOT_FOUND"}
            if current < lease["renewed_at"]:
                return {"renewed": False, "status": "INVALID_CONCURRENCY_TIMESTAMP"}
            if lease["expires_at"] <= current:
                state["leases"].pop(lease_token, None)
                try:
                    self._save(state)
                except OSError:
                    return {"renewed": False, "status": "CONCURRENCY_PERSISTENCE_FAILED"}
                return {"renewed": False, "status": "CONCURRENCY_LEASE_EXPIRED"}
            lease["renewed_at"] = current
            lease["expires_at"] = current + self.lease_seconds
            try:
                self._save(state)
            except OSError:
                return {"renewed": False, "status": "CONCURRENCY_PERSISTENCE_FAILED"}
            return {"renewed": True, "status": "CONCURRENCY_LEASE_RENEWED", "expires_at": lease["expires_at"]}

    def release(self, lease_token: str) -> dict[str, Any]:
        if not self._valid_id(lease_token):
            return {"released": False, "status": "INVALID_CONCURRENCY_LEASE"}
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"released": False, "status": "CONCURRENCY_STATE_INVALID"}
            lease = state["leases"].pop(lease_token, None)
            if lease is None:
                return {"released": True, "status": "ALREADY_RELEASED"}
            try:
                self._save(state)
            except OSError:
                return {"released": False, "status": "CONCURRENCY_PERSISTENCE_FAILED"}
            return {"released": True, "status": "PROVIDER_CONCURRENCY_LEASE_RELEASED"}

    @contextmanager
    def maintain_lease(self, lease_token: str) -> Iterator[threading.Event]:
        """Renew an active lease in the background while a provider call runs."""
        lost = threading.Event()
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(self.heartbeat_interval):
                result = self.renew(lease_token)
                if not isinstance(result, dict) or result.get("renewed") is not True:
                    lost.set()
                    return

        thread = threading.Thread(
            target=heartbeat, name="provider-concurrency-heartbeat", daemon=True,
        )
        thread.start()
        try:
            yield lost
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_interval * 2))

    def get_status(self, provider_id: str, *, now: float | None = None) -> dict[str, Any]:
        if not self._valid_id(provider_id):
            return {"status": "INVALID_PROVIDER_ID"}
        try:
            current = self._now(now)
        except (TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CONCURRENCY_TIMESTAMP"}
        with self._lock, interprocess_file_lock(str(self.path) + ".lock"):
            try:
                state = self._load()
            except ValueError:
                return {"status": "CONCURRENCY_STATE_INVALID"}
            leases = state["leases"]
            self._purge_expired(leases, current)
            active = [lease for lease in leases.values() if lease["provider_id"] == provider_id]
            return {
                "status": "PROVIDER_CONCURRENCY_STATUS",
                "provider_id": provider_id,
                "active": len(active),
                "limit": self.provider_limits.get(provider_id, self.default_limit),
                "leases": [{
                    "project_id": lease["project_id"], "tenant_id": lease["tenant_id"],
                    "request_id": lease["request_id"], "worker_id": lease["worker_id"],
                    "expires_at": lease["expires_at"],
                } for lease in active],
            }
