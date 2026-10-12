"""Persistent provider health history, probes, and eligibility decisions."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderHealthManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        history_limit: int = 50,
        failure_threshold: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(history_limit, int) or isinstance(history_limit, bool) or history_limit < 1
            or not isinstance(failure_threshold, int) or isinstance(failure_threshold, bool) or failure_threshold < 1
        ):
            raise ValueError("history_limit and failure_threshold must be positive integers")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.history_limit = history_limit
        self.failure_threshold = failure_threshold
        self._lock = threading.RLock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.data: dict[str, Any] = {"version": "1.0", "providers": {}}
        self._reload_state()

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid provider health timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid provider health timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _reload_state(self) -> None:
        """Reload the latest valid state while holding the interprocess lock."""
        if not self.state_path.exists():
            self.data = {"version": "1.0", "providers": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid provider health state JSON") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers", {}), dict):
            raise ValueError("Invalid provider health state")
        loaded.setdefault("providers", {})
        for provider_id, record in loaded["providers"].items():
            if not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(record, dict):
                raise ValueError("Invalid provider health record")
            status = record.get("status")
            if status is not None and status not in {"HEALTHY", "DEGRADED", "UNHEALTHY"}:
                raise ValueError("Invalid provider health status")
            failures = record.get("consecutive_failures", 0)
            if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
                raise ValueError("Invalid provider consecutive failure count")
            history = record.get("history", [])
            if not isinstance(history, list):
                raise ValueError("Invalid provider health history")
            for event in history:
                if not isinstance(event, dict) or not isinstance(event.get("success"), bool):
                    raise ValueError("Invalid provider health history event")
                self._parse_timestamp(event.get("timestamp"))
                latency = event.get("latency_ms")
                if latency is not None and (
                    not isinstance(latency, (int, float))
                    or isinstance(latency, bool)
                    or not math.isfinite(latency)
                    or latency < 0
                ):
                    raise ValueError("Invalid provider health latency")
                error_code = event.get("error_code")
                if error_code is not None and (
                    not isinstance(error_code, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", error_code)
                ):
                    raise ValueError("Invalid provider health error code")
            for timestamp_key in ("last_checked_at", "last_success_at", "last_failure_at"):
                if timestamp_key in record:
                    self._parse_timestamp(record[timestamp_key])
            latency = record.get("last_latency_ms")
            if latency is not None and (
                not isinstance(latency, (int, float))
                or isinstance(latency, bool)
                or not math.isfinite(latency)
                or latency < 0
            ):
                raise ValueError("Invalid provider last health latency")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize health history updates across threads and processes."""
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()
                yield

    def record_result(
        self,
        provider_id: str,
        *,
        success: bool,
        latency_ms: float | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "saved": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_RESULT", "saved": False}
        if latency_ms is not None and (
            not isinstance(latency_ms, (int, float))
            or isinstance(latency_ms, bool)
            or not math.isfinite(latency_ms)
            or latency_ms < 0
        ):
            return {"status": "INVALID_LATENCY", "saved": False}
        with self._transaction():
            providers = self.data["providers"]
            current = providers.get(provider_id, {})
            record = dict(current) if isinstance(current, dict) else {}
            history = list(record.get("history", []))
            event = {
                "success": success,
                "timestamp": self.clock().astimezone(timezone.utc).isoformat(),
            }
            if latency_ms is not None:
                event["latency_ms"] = round(float(latency_ms), 2)
            if not success and error_code:
                # Only store compact machine-readable codes, never arbitrary exception text.
                candidate_code = str(error_code).strip()
                event["error_code"] = (
                    candidate_code
                    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", candidate_code)
                    else "UNCLASSIFIED_ERROR"
                )
            history.append(event)
            history = history[-self.history_limit:]
            consecutive_failures = 0 if success else int(record.get("consecutive_failures", 0)) + 1
            if success:
                status = "HEALTHY"
            elif consecutive_failures >= self.failure_threshold:
                status = "UNHEALTHY"
            else:
                status = "DEGRADED"
            record.update({
                "status": status,
                "history": history,
                "consecutive_failures": consecutive_failures,
                "last_checked_at": event["timestamp"],
            })
            if success:
                record["last_success_at"] = event["timestamp"]
                record["last_latency_ms"] = event.get("latency_ms")
            else:
                record["last_failure_at"] = event["timestamp"]
            providers[provider_id] = record
            self._save()
            return {"provider": provider_id, "status": status, "saved": True, "history_size": len(history)}

    def active_probe(self, provider_id: str, probe: Any, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"provider": provider_id, "status": "INVALID_PROVIDER", "verified": False}
        if not callable(probe):
            return {"provider": provider_id, "status": "INVALID_PROBE", "verified": False}
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            return {"provider": provider_id, "status": "INVALID_TIMEOUT", "verified": False}
        started = __import__("time").monotonic()
        try:
            result = probe(timeout_seconds=float(timeout_seconds))
        except Exception as exc:
            name = type(exc).__name__.casefold()
            code = "TIMEOUT" if isinstance(exc, TimeoutError) or "timeout" in name else "NETWORK_ERROR"
            try:
                recorded = self.record_result(provider_id, success=False, error_code=code)
            except Exception as persist_exc:
                return {
                    "provider": provider_id,
                    "status": "HEALTH_RECORD_FAILED",
                    "verified": False,
                    "error_type": type(persist_exc).__name__,
                }
            if not isinstance(recorded, dict) or recorded.get("saved") is not True:
                return {"provider": provider_id, "status": "HEALTH_RECORD_FAILED", "verified": False}
            return {"provider": provider_id, "status": code, "verified": False, "error_type": type(exc).__name__}
        if result is not True and not (isinstance(result, dict) and result.get("ok") is True):
            try:
                recorded = self.record_result(provider_id, success=False, error_code="PROBE_FAILED")
            except Exception as exc:
                return {
                    "provider": provider_id,
                    "status": "HEALTH_RECORD_FAILED",
                    "verified": False,
                    "error_type": type(exc).__name__,
                }
            if not isinstance(recorded, dict) or recorded.get("saved") is not True:
                return {"provider": provider_id, "status": "HEALTH_RECORD_FAILED", "verified": False}
            return {"provider": provider_id, "status": "PROBE_FAILED", "verified": False}
        latency = (__import__("time").monotonic() - started) * 1000
        try:
            recorded = self.record_result(provider_id, success=True, latency_ms=latency)
        except Exception as exc:
            return {
                "provider": provider_id,
                "status": "HEALTH_RECORD_FAILED",
                "verified": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(recorded, dict) or recorded.get("saved") is not True:
            return {"provider": provider_id, "status": "HEALTH_RECORD_FAILED", "verified": False}
        return {"provider": provider_id, "status": "HEALTHY", "verified": True, "latency_ms": round(latency, 2)}

    def is_eligible(
        self,
        provider_id: str,
        *,
        stale_after_seconds: float = 300.0,
        max_latency_ms: float | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(stale_after_seconds, (int, float))
            or isinstance(stale_after_seconds, bool)
            or not math.isfinite(stale_after_seconds)
            or stale_after_seconds < 0
        ):
            return {"eligible": False, "status": "INVALID_HEALTH_POLICY", "reason": "INVALID_STALE_LIMIT"}
        if max_latency_ms is not None and (
            not isinstance(max_latency_ms, (int, float))
            or isinstance(max_latency_ms, bool)
            or not math.isfinite(max_latency_ms)
            or max_latency_ms < 0
        ):
            return {"eligible": False, "status": "INVALID_HEALTH_POLICY", "reason": "INVALID_LATENCY_LIMIT"}
        with self._transaction():
            current = self.data["providers"].get(provider_id)
            record = dict(current) if isinstance(current, dict) else None
        if not isinstance(record, dict) or not record.get("last_checked_at"):
            return {"eligible": False, "status": "UNKNOWN", "reason": "NO_HEALTH_EVIDENCE"}
        try:
            checked = datetime.fromisoformat(record["last_checked_at"])
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=timezone.utc)
            age = (self.clock().astimezone(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return {"eligible": False, "status": "UNKNOWN", "reason": "INVALID_HEALTH_TIMESTAMP"}
        if age < 0:
            return {"eligible": False, "status": "UNKNOWN", "reason": "FUTURE_HEALTH_TIMESTAMP", "age_seconds": age}
        if age > stale_after_seconds:
            return {"eligible": False, "status": "STALE", "reason": "HEALTH_CHECK_STALE", "age_seconds": age}
        if record.get("status") == "UNHEALTHY":
            return {"eligible": False, "status": "UNHEALTHY", "reason": "FAILURE_THRESHOLD_REACHED"}
        latency = record.get("last_latency_ms")
        if max_latency_ms is not None and latency is not None and latency > max_latency_ms:
            return {"eligible": False, "status": "SLOW", "reason": "LATENCY_LIMIT_EXCEEDED"}
        return {"eligible": True, "status": record.get("status", "UNKNOWN"), "age_seconds": age}

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
