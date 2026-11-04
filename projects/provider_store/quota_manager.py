"""Persistent, cross-process concurrency-safe quotas with reserve/commit/release."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from typing import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import re

from .encrypted_state_store import EncryptedJSONStateStore
from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderQuotaManager:
    MAX_ATTEMPT_HISTORY = 1000
    TENANT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        limits: dict[str, dict[str, int]],
        clock: Callable[[], datetime] | None = None,
        max_records: int = 100_000,
        tenant_limits: dict[str, dict[str, dict[str, int]]] | None = None,
        max_tenants: int = 10_000,
        state_encryption_key_provider: Any = None,
        state_encryption_context: str = "provider-quota-state-v1",
        migrate_legacy_plaintext_state: bool = False,
    ) -> None:
        if state_encryption_key_provider is None and migrate_legacy_plaintext_state:
            raise ValueError("legacy state migration requires an encryption key provider")
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
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 1_000_000:
            raise ValueError("max_records must be within 1..1000000")
        if isinstance(max_tenants, bool) or not isinstance(max_tenants, int) or not 1 <= max_tenants <= 100_000:
            raise ValueError("max_tenants must be within 1..100000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.state_store = (
            EncryptedJSONStateStore(
                self.state_path,
                key_provider=state_encryption_key_provider,
                context=state_encryption_context,
                migrate_legacy_plaintext=migrate_legacy_plaintext_state,
            )
            if state_encryption_key_provider is not None else None
        )
        self.limits = normalized
        self.max_records = max_records
        self.max_tenants = max_tenants
        self.tenant_limits = self._normalize_tenant_limits(tenant_limits or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "requests": {}}
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        with interprocess_file_lock(lock_path):
            self._reload_state()
            if self.state_store is not None and self.state_store.migration_required:
                self._save()

    def _normalize_tenant_limits(
        self, configuration: dict[str, dict[str, dict[str, int]]]
    ) -> dict[str, dict[str, dict[str, int]]]:
        if not isinstance(configuration, dict) or len(configuration) > self.max_tenants:
            raise ValueError("invalid tenant quota policy configuration")
        normalized: dict[str, dict[str, dict[str, int]]] = {}
        for tenant_id, providers in configuration.items():
            if not isinstance(tenant_id, str) or self.TENANT_ID_RE.fullmatch(tenant_id) is None:
                raise ValueError("invalid tenant quota policy identifier")
            if not isinstance(providers, dict):
                raise ValueError("invalid tenant provider quota configuration")
            normalized[tenant_id] = {}
            for provider_id, periods in providers.items():
                if not isinstance(provider_id, str) or provider_id not in self.limits or not isinstance(periods, dict):
                    raise ValueError("tenant quota references an unknown provider")
                normalized[tenant_id][provider_id] = {}
                for period, limit in periods.items():
                    if not isinstance(period, str) or period not in {"hourly", "daily", "monthly"}:
                        raise ValueError("unsupported tenant quota period")
                    if period not in self.limits[provider_id]:
                        raise ValueError("tenant quota period has no global provider limit")
                    if (
                        isinstance(limit, bool) or not isinstance(limit, int)
                        or limit < 1 or limit > self.limits[provider_id][period]
                    ):
                        raise ValueError("tenant quota must be positive and no greater than global limit")
                    normalized[tenant_id][provider_id][period] = limit
        return normalized

    @classmethod
    def _valid_tenant_id(cls, tenant_id: Any) -> bool:
        return isinstance(tenant_id, str) and cls.TENANT_ID_RE.fullmatch(tenant_id) is not None

    def _reload_state(self) -> None:
        """Reload the latest atomically written state while holding the process lock."""
        default = {"version": "1.0", "requests": {}}
        if self.state_store is not None:
            loaded = self.state_store.read(default)
            if not self.state_path.exists():
                self.data = loaded
                return
        else:
            if not self.state_path.exists():
                self.data = default
                return
            try:
                with self.state_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("invalid quota state JSON") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("requests"), dict):
            raise ValueError("invalid quota state schema")
        if len(loaded["requests"]) > self.max_records:
            raise ValueError("quota request state exceeds configured record limit")
        for request_id, record in loaded["requests"].items():
            if (
                not isinstance(request_id, str)
                or not isinstance(record, dict)
                or not isinstance(record.get("provider_id"), str)
                or record.get("status") not in {"RESERVED", "COMMITTED", "RELEASED"}
            ):
                raise ValueError("invalid quota request record")
            self._parse_timestamp(record.get("created_at"))
            if record.get("status") == "COMMITTED":
                self._parse_timestamp(record.get("committed_at"))
            if record.get("status") == "RELEASED":
                self._parse_timestamp(record.get("released_at"))
            tenant_id = record.get("tenant_id")
            if tenant_id is not None and not self._valid_tenant_id(tenant_id):
                raise ValueError("invalid quota tenant identifier")
            history = record.get("attempt_history", [])
            if (
                not isinstance(history, list)
                or len(history) > self.MAX_ATTEMPT_HISTORY
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("provider_id"), str)
                    or not item.get("provider_id", "").strip()
                    or item.get("status") != "RELEASED"
                    for item in history
                )
            ):
                raise ValueError("invalid quota attempt history")
            for item in history:
                self._parse_timestamp(item.get("created_at"))
                self._parse_timestamp(item.get("released_at"))
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize mutations across threads and OS processes, refreshing state first."""
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()
                yield

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

    def reserve(
        self, *, provider_id: str, request_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip() or not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "reserved": False}
        if tenant_id is not None and not self._valid_tenant_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "reserved": False}
        if self.tenant_limits and tenant_id is None:
            return {"status": "TENANT_CONTEXT_REQUIRED", "reserved": False}
        if provider_id not in self.limits:
            return {"status": "NO_QUOTA_POLICY", "provider": provider_id, "reserved": False}
        with self._transaction():
            existing = self.data["requests"].get(request_id)
            now = self.clock().astimezone(timezone.utc)
            if existing is not None:
                if existing.get("tenant_id") != tenant_id:
                    return {"status": "TENANT_ISOLATION_DENIED", "request_id": request_id, "reserved": False}
                if existing.get("status") == "RELEASED" and existing.get("provider_id") != provider_id:
                    # A released attempt is known not to have started; preserve its
                    # audit snapshot and permit a new provider attempt under the same
                    # logical request ID. Started/committed attempts cannot be reused.
                    history = existing.setdefault("attempt_history", [])
                    if not isinstance(history, list):
                        return {"status": "INVALID_ATTEMPT_HISTORY", "reserved": False}
                    if len(history) >= self.MAX_ATTEMPT_HISTORY:
                        return {"status": "REQUEST_ATTEMPT_HISTORY_FULL", "reserved": False}
                    history.append({
                        "provider_id": existing.get("provider_id"),
                        "status": "RELEASED",
                        "created_at": existing.get("created_at"),
                        "released_at": existing.get("released_at"),
                    })
                    existing["provider_id"] = provider_id
                    existing["status"] = "RESERVED"
                    existing["created_at"] = now.isoformat()
                    existing.pop("released_at", None)
                    self._save()
                    return {
                        "status": "RESERVED", "provider": provider_id,
                        "request_id": request_id, "reserved": True,
                        "reused_released_attempt": True,
                    }
                if existing.get("provider_id") != provider_id:
                    return {"status": "REQUEST_ID_CONFLICT", "request_id": request_id, "reserved": False}
                existing_status = existing["status"]
                status = {
                    "RESERVED": "ALREADY_RESERVED",
                    "COMMITTED": "ALREADY_COMMITTED",
                    "RELEASED": "ALREADY_RELEASED",
                }[existing_status]
                return {"status": status, "request_id": request_id, "reserved": False}
            if len(self.data["requests"]) >= self.max_records:
                # Keep idempotency/audit records: never evict old request IDs
                # merely to make room for a new potentially billable attempt.
                return {
                    "status": "QUOTA_STATE_CAPACITY_REACHED",
                    "provider": provider_id,
                    "reserved": False,
                }
            for period, limit in self.limits[provider_id].items():
                count = self._count_in_period(provider_id, period, now)
                if tenant_id is not None:
                    tenant_limit = self.tenant_limits.get(tenant_id, {}).get(provider_id, {}).get(period, limit)
                    tenant_used = self._count_tenant_in_period(tenant_id, provider_id, period, now)
                    if tenant_used >= tenant_limit:
                        return {
                            "status": "TENANT_QUOTA_EXCEEDED",
                            "tenant_id": tenant_id,
                            "provider": provider_id,
                            "period": period,
                            "limit": tenant_limit,
                            "used": tenant_used,
                            "reserved": False,
                        }
                if count >= limit:
                    return {
                        "status": "QUOTA_EXCEEDED",
                        "provider": provider_id,
                        "period": period,
                        "limit": limit,
                        "used": count,
                        "reserved": False,
                    }
            record = {
                "provider_id": provider_id,
                "status": "RESERVED",
                "created_at": now.isoformat(),
            }
            if tenant_id is not None:
                record["tenant_id"] = tenant_id
            self.data["requests"][request_id] = record
            self._save()
            return {"status": "RESERVED", "provider": provider_id, "request_id": request_id, "reserved": True}

    def commit(
        self, *, request_id: str, tenant_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "committed": False}
        if tenant_id is not None and not self._valid_tenant_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "committed": False}
        with self._transaction():
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "RESERVATION_NOT_FOUND", "committed": False}
            if record.get("tenant_id") != tenant_id:
                return {"status": "TENANT_ISOLATION_DENIED", "committed": False}
            if record.get("status") == "COMMITTED":
                return {"status": "ALREADY_COMMITTED", "committed": False}
            if record.get("status") != "RESERVED":
                return {"status": "INVALID_RESERVATION_STATE", "committed": False}
            record["status"] = "COMMITTED"
            record["committed_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": "COMMITTED", "request_id": request_id, "committed": True}

    def release(
        self, *, request_id: str, provider_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST", "released": False}
        if tenant_id is not None and not self._valid_tenant_id(tenant_id):
            return {"status": "INVALID_TENANT_ID", "released": False}
        with self._transaction():
            record = self.data["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "RESERVATION_NOT_FOUND", "released": False}
            if record.get("tenant_id") != tenant_id:
                return {"status": "TENANT_ISOLATION_DENIED", "released": False}
            if provider_id is not None and record.get("provider_id") != provider_id:
                return {"status": "REQUEST_PROVIDER_MISMATCH", "released": False}
            if record.get("status") == "COMMITTED":
                return {"status": "ALREADY_COMMITTED", "released": False}
            if record.get("status") == "RELEASED":
                return {"status": "RELEASED", "request_id": request_id, "released": True, "already_released": True}
            if record.get("status") != "RESERVED":
                return {"status": "INVALID_RESERVATION_STATE", "released": False}
            record["status"] = "RELEASED"
            record["released_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": "RELEASED", "request_id": request_id, "released": True}

    def usage(self, *, provider_id: str) -> dict[str, int]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {}
        with self._transaction():
            now = self.clock().astimezone(timezone.utc)
            return {
                period: self._count_in_period(provider_id, period, now)
                for period in self.limits.get(provider_id, {})
            }

    def tenant_usage(self, *, tenant_id: str, provider_id: str) -> dict[str, int]:
        """Return committed plus reserved request counts for one tenant/provider."""
        if not self._valid_tenant_id(tenant_id) or not isinstance(provider_id, str) or not provider_id.strip():
            return {}
        with self._transaction():
            now = self.clock().astimezone(timezone.utc)
            return {
                period: self._count_tenant_in_period(tenant_id, provider_id, period, now)
                for period in self.limits.get(provider_id, {})
            }

    def _count_tenant_in_period(
        self, tenant_id: str, provider_id: str, period: str, now: datetime
    ) -> int:
        if period == "monthly":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "daily":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now.replace(minute=0, second=0, microsecond=0)
        count = 0
        for record in self.data["requests"].values():
            if (
                not isinstance(record, dict)
                or record.get("tenant_id") != tenant_id
                or record.get("provider_id") != provider_id
                or record.get("status") == "RELEASED"
            ):
                continue
            try:
                created = self._parse_timestamp(record.get("created_at"))
            except ValueError:
                continue
            if start <= created <= now:
                count += 1
        return count

    def _count_in_period(self, provider_id: str, period: str, now: datetime) -> int:
        if period == "monthly":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "daily":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now.replace(minute=0, second=0, microsecond=0)
        count = 0
        for record in self.data["requests"].values():
            if (
                not isinstance(record, dict)
                or record.get("provider_id") != provider_id
                or record.get("status") == "RELEASED"
            ):
                continue
            try:
                created = self._parse_timestamp(record.get("created_at"))
            except ValueError:
                continue
            if start <= created <= now:
                count += 1
        return count

    def _save(self) -> None:
        if self.state_store is not None:
            self.state_store.write(self.data)
            return
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
