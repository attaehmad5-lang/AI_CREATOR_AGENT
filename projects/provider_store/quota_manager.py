"""Persistent, concurrency-safe provider request quotas with reserve/commit/release."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class ProviderQuotaManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        limits: dict[str, dict[str, int]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(limits, dict):
            raise ValueError("limits must be a provider-to-limit mapping")
        normalized: dict[str, dict[str, int]] = {}
        for provider_id, provider_limits in limits.items():
            if not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(provider_limits, dict):
                raise ValueError("invalid provider quota configuration")
            normalized[provider_id] = {}
            for period, limit in provider_limits.items():
                if period not in {"hourly", "daily", "monthly"}:
                    raise ValueError("unsupported quota period")
                if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                    raise ValueError("quota limits must be positive integers")
                normalized[provider_id][period] = limit
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.limits = normalized
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "requests": {}}
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("invalid quota state JSON") from exc
            if not isinstance(loaded, dict) or not isinstance(loaded.get("requests"), dict):
                raise ValueError("invalid quota state schema")
            for request_id, record in loaded["requests"].items():
                if (
                    not isinstance(request_id, str)
                    or not isinstance(record, dict)
                    or not isinstance(record.get("provider_id"), str)
                    or record.get("status") not in {"RESERVED", "COMMITTED"}
                ):
                    raise ValueError("invalid quota request record")
                self._parse_timestamp(record.get("created_at"))
            self.data = loaded

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid quota timestamp")
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid quota timestamp") from exc
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)

    def reserve(self, *, provider_id: str, request_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "reserved": False}
        if provider_id not in self.limits:
            return {"status": "NO_QUOTA_POLICY", "provider": provider_id, "reserved": False}
        with self._lock:
            existing = self.data["requests"].get(request_id)
            if existing is not None:
                if existing.get("provider_id") != provider_id:
                    return {"status": "REQUEST_ID_CONFLICT", "request_id": request_id, "reserved": False}
                return {
                    "status": "ALREADY_RESERVED" if existing["status"] == "RESERVED" else "ALREADY_COMMITTED",
                    "request_id": request_id,
                    "reserved": False,
                }
            now = self.clock().astimezone(timezone.utc)
            for period, limit in self.limits[provider_id].items():
                count = self._count_in_period(provider_id, period, now)
                if count >= limit:
                    return {
                        "status": "QUOTA_EXCEEDED",
                        "provider": provider_id,
                        "period": period,
                        "limit": limit,
                        "used": count,
                        "reserved": False,
                    }
            self.data["requests"][request_id] = {
                "provider_id": provider_id,
                "status": "RESERVED",
                "created_at": now.isoformat(),
            }
            self._save()
            return {"status": "RESERVED", "provider": provider_id, "request_id": request_id, "reserved": True}

    def commit(self, *, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "committed": False}
        with self._lock:
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "RESERVATION_NOT_FOUND", "committed": False}
            if record.get("status") == "COMMITTED":
                return {"status": "ALREADY_COMMITTED", "committed": False}
            if record.get("status") != "RESERVED":
                return {"status": "INVALID_RESERVATION_STATE", "committed": False}
            record["status"] = "COMMITTED"
            record["committed_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": "COMMITTED", "request_id": request_id, "committed": True}

    def release(self, *, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "released": False}
        with self._lock:
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "RESERVATION_NOT_FOUND", "released": False}
            if record.get("status") == "COMMITTED":
                return {"status": "ALREADY_COMMITTED", "released": False}
            if record.get("status") != "RESERVED":
                return {"status": "INVALID_RESERVATION_STATE", "released": False}
            del self.data["requests"][request_id]
            self._save()
            return {"status": "RELEASED", "request_id": request_id, "released": True}

    def usage(self, *, provider_id: str) -> dict[str, int]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {}
        now = self.clock().astimezone(timezone.utc)
        return {
            period: self._count_in_period(provider_id, period, now)
            for period in self.limits.get(provider_id, {})
        }

    def _count_in_period(self, provider_id: str, period: str, now: datetime) -> int:
        if period == "monthly":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "daily":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now.replace(minute=0, second=0, microsecond=0)
        count = 0
        for record in self.data["requests"].values():
            if not isinstance(record, dict) or record.get("provider_id") != provider_id:
                continue
            try:
                created = self._parse_timestamp(record.get("created_at"))
            except ValueError:
                continue
            if start <= created <= now:
                count += 1
        return count

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
            os.replace(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
