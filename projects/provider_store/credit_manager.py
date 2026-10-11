"""Fresh, verified provider free-credit snapshots supplied by provider-specific probes."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .interprocess_lock import interprocess_file_lock


class ProviderCreditManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_age_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(max_age_seconds, (int, float))
            or isinstance(max_age_seconds, bool)
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("max_age_seconds must be a finite positive number")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_age_seconds = float(max_age_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "providers": {}, "reservations": {}, "usage": {}}
        self._reload_state()

    def _reload_state(self) -> None:
        """Reload current credit snapshots while holding the interprocess lock."""
        if not self.state_path.exists():
            self.data = {"version": "1.0", "providers": {}, "reservations": {}, "usage": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider credit state JSON") from exc
        if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), dict):
            raise ValueError("invalid provider credit state schema")
        loaded.setdefault("reservations", {})
        loaded.setdefault("usage", {})
        if not isinstance(loaded["reservations"], dict) or not isinstance(loaded["usage"], dict):
            raise ValueError("invalid provider credit reservation/usage state")
        for provider_id, record in loaded["providers"].items():
            if (
                not isinstance(provider_id, str) or not provider_id.strip()
                or not isinstance(record, dict)
                or not self._valid_amount(record.get("free_credits_remaining"))
                or not isinstance(record.get("verified"), bool)
                or not isinstance(record.get("unit", "credits"), str)
            ):
                raise ValueError("invalid provider credit snapshot")
            self._parse_timestamp(record.get("observed_at"))
            version = record.get("snapshot_version", 1)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValueError("invalid provider credit snapshot version")
            record.setdefault("snapshot_version", 1)
        for request_id, record in {**loaded["reservations"], **loaded["usage"]}.items():
            if (
                not isinstance(request_id, str) or not request_id.strip()
                or not isinstance(record, dict)
                or not isinstance(record.get("provider_id"), str) or not record["provider_id"].strip()
                or not self._valid_amount(record.get("amount"))
                or not isinstance(record.get("unit"), str) or not record["unit"].strip()
            ):
                raise ValueError("invalid provider credit usage record")
            timestamp_key = "created_at" if request_id in loaded["reservations"] else "committed_at"
            self._parse_timestamp(record.get(timestamp_key))
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize credit snapshot reads/writes across threads and processes."""
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()
                yield

    @staticmethod
    def _valid_amount(value: Any):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("invalid credit observation timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid credit observation timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def refresh(self, provider_id: str, probe: Any) -> dict[str, Any]:
        """Call an injected provider-specific account-balance probe; never infer credits."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "verified": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "provider": provider_id, "verified": False}
        try:
            result = probe(provider_id)
        except Exception as exc:
            return {
                "status": "CREDIT_PROBE_FAILED",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or result.get("verified") is not True:
            return {"status": "UNVERIFIED_CREDIT_BALANCE", "provider": provider_id, "verified": False}
        amount = result.get("free_credits_remaining")
        if not self._valid_amount(amount):
            return {"status": "INVALID_CREDIT_AMOUNT", "provider": provider_id, "verified": False}
        source = result.get("source")
        if not isinstance(source, str) or not source.strip() or len(source) > 120:
            return {"status": "INVALID_CREDIT_SOURCE", "provider": provider_id, "verified": False}
        unit = result.get("unit", "credits")
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
            return {"status": "INVALID_CREDIT_UNIT", "provider": provider_id, "verified": False}
        observed_at = result.get("observed_at")
        if observed_at is None:
            observed = self.clock().astimezone(timezone.utc)
        else:
            try:
                observed = self._parse_timestamp(observed_at)
            except ValueError:
                return {"status": "INVALID_CREDIT_TIMESTAMP", "provider": provider_id, "verified": False}
        now = self.clock().astimezone(timezone.utc)
        if observed > now:
            return {"status": "FUTURE_CREDIT_TIMESTAMP", "provider": provider_id, "verified": False}
        record = {
            "free_credits_remaining": float(amount),
            "unit": unit.strip(),
            "source": source.strip(),
            "verified": True,
            "observed_at": observed.isoformat(),
        }
        with self._transaction():
            if any(
                item.get("provider_id") == provider_id
                for item in self.data["reservations"].values()
                if isinstance(item, dict)
            ):
                return {
                    "status": "CREDIT_RESERVATIONS_IN_FLIGHT",
                    "provider": provider_id,
                    "verified": False,
                }
            current_snapshot = self.data["providers"].get(provider_id, {})
            current_version = current_snapshot.get("snapshot_version", 1) if isinstance(current_snapshot, dict) else 1
            for item in self.data["usage"].values():
                if (
                    isinstance(item, dict)
                    and item.get("provider_id") == provider_id
                    and item.get("snapshot_version", 1) == current_version
                ):
                    try:
                        committed_at = self._parse_timestamp(item.get("committed_at"))
                    except ValueError:
                        return {"status": "INVALID_LOCAL_USAGE_TIMESTAMP", "provider": provider_id, "verified": False}
                    if observed <= committed_at:
                        return {
                            "status": "CREDIT_SNAPSHOT_PREDATES_LOCAL_USAGE",
                            "provider": provider_id,
                            "verified": False,
                        }
            record["snapshot_version"] = int(current_version) + 1
            self.data["providers"][provider_id] = record
            self._save()
        return {"status": "CREDIT_BALANCE_UPDATED", "provider": provider_id, "verified": True, "observed_at": record["observed_at"]}

    def get_balance(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "verified": False}
        with self._transaction():
            current = self.data["providers"].get(provider_id)
            record = dict(current) if isinstance(current, dict) else None
        if not isinstance(record, dict):
            return {"status": "CREDIT_BALANCE_UNKNOWN", "provider": provider_id, "verified": False}
        if record.get("verified") is not True:
            return {"status": "UNVERIFIED_CREDIT_BALANCE", "provider": provider_id, "verified": False}
        try:
            observed = self._parse_timestamp(record.get("observed_at"))
            now = self.clock().astimezone(timezone.utc)
        except (ValueError, TypeError):
            return {"status": "INVALID_CREDIT_TIMESTAMP", "provider": provider_id, "verified": False}
        age = (now - observed).total_seconds()
        if age < 0:
            return {"status": "FUTURE_CREDIT_TIMESTAMP", "provider": provider_id, "verified": False}
        if age > self.max_age_seconds:
            return {"status": "CREDIT_BALANCE_STALE", "provider": provider_id, "verified": False, "age_seconds": age}
        return {
            "status": "CREDIT_BALANCE_FRESH",
            "provider": provider_id,
            "verified": True,
            "free_credits_remaining": record["free_credits_remaining"],
            "unit": record.get("unit", "credits"),
            "source": record.get("source"),
            "age_seconds": age,
        }

    def usable_free_credits(self, *, unit: str | None = None) -> dict[str, float]:
        """Return fresh verified, unit-compatible balances after local reservations/usage."""
        if unit is not None and (
            not isinstance(unit, str) or not unit.strip() or len(unit) > 32
        ):
            return {}
        result: dict[str, float] = {}
        with self._transaction():
            now = self.clock().astimezone(timezone.utc)
            for provider_id, record in self.data["providers"].items():
                if record.get("verified") is not True or (unit is not None and record.get("unit") != unit.strip()):
                    continue
                try:
                    observed = self._parse_timestamp(record.get("observed_at"))
                except ValueError:
                    continue
                age = (now - observed).total_seconds()
                if age < 0 or age > self.max_age_seconds:
                    continue
                amount = float(record["free_credits_remaining"])
                snapshot_version = record.get("snapshot_version", 1)
                amount -= sum(
                    float(item["amount"]) for item in self.data["usage"].values()
                    if isinstance(item, dict) and item.get("provider_id") == provider_id
                    and item.get("unit") == record.get("unit")
                    and item.get("snapshot_version", 1) == snapshot_version
                )
                amount -= sum(
                    float(item["amount"]) for item in self.data["reservations"].values()
                    if isinstance(item, dict) and item.get("provider_id") == provider_id
                    and item.get("unit") == record.get("unit")
                )
                result[provider_id] = max(0.0, amount)
        return result

    def reserve(self, *, provider_id: str, amount: float, request_id: str, unit: str) -> dict[str, Any]:
        """Reserve verified free credits atomically before starting a provider request."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "reserved": False}
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "reserved": False}
        if not self._valid_amount(amount) or amount <= 0:
            return {"status": "INVALID_CREDIT_AMOUNT", "reserved": False}
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
            return {"status": "INVALID_CREDIT_UNIT", "reserved": False}
        with self._transaction():
            if request_id in self.data["usage"]:
                existing = self.data["usage"][request_id]
                if (
                    isinstance(existing, dict) and existing.get("provider_id") == provider_id
                    and existing.get("amount") == float(amount) and existing.get("unit") == unit.strip()
                ):
                    return {"status": "ALREADY_COMMITTED", "reserved": False}
                return {"status": "REQUEST_ID_CONFLICT", "reserved": False}
            existing = self.data["reservations"].get(request_id)
            if existing is not None:
                if (
                    isinstance(existing, dict) and existing.get("provider_id") == provider_id
                    and existing.get("amount") == float(amount) and existing.get("unit") == unit.strip()
                ):
                    return {"status": "ALREADY_RESERVED", "reserved": False}
                return {"status": "REQUEST_ID_CONFLICT", "reserved": False}
            snapshot = self.data["providers"].get(provider_id)
            if not isinstance(snapshot, dict) or snapshot.get("verified") is not True:
                return {"status": "CREDIT_BALANCE_UNKNOWN", "reserved": False}
            if snapshot.get("unit", "credits") != unit.strip():
                return {"status": "CREDIT_UNIT_MISMATCH", "reserved": False}
            try:
                age = (self.clock().astimezone(timezone.utc) - self._parse_timestamp(snapshot.get("observed_at"))).total_seconds()
            except ValueError:
                return {"status": "INVALID_CREDIT_TIMESTAMP", "reserved": False}
            if age < 0:
                return {"status": "FUTURE_CREDIT_TIMESTAMP", "reserved": False}
            if age > self.max_age_seconds:
                return {"status": "CREDIT_BALANCE_STALE", "reserved": False}
            snapshot_version = snapshot.get("snapshot_version", 1)
            used = sum(
                float(item["amount"]) for item in self.data["usage"].values()
                if isinstance(item, dict) and item.get("provider_id") == provider_id
                and item.get("unit") == unit.strip()
                and item.get("snapshot_version", 1) == snapshot_version
            )
            reserved = sum(
                float(item["amount"]) for item in self.data["reservations"].values()
                if isinstance(item, dict) and item.get("provider_id") == provider_id and item.get("unit") == unit.strip()
            )
            remaining = max(0.0, float(snapshot["free_credits_remaining"]) - used - reserved)
            if amount > remaining:
                return {"status": "INSUFFICIENT_FREE_CREDITS", "reserved": False, "available": remaining}
            self.data["reservations"][request_id] = {
                "provider_id": provider_id,
                "amount": float(amount),
                "unit": unit.strip(),
                "created_at": self.clock().astimezone(timezone.utc).isoformat(),
            }
            self._save()
            return {"status": "RESERVED", "reserved": True, "request_id": request_id, "available": remaining - float(amount)}

    def commit(self, *, request_id: str, actual_amount: float | None = None) -> dict[str, Any]:
        """Commit reserved credit usage once the provider request has started."""
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "committed": False}
        if actual_amount is not None and not self._valid_amount(actual_amount):
            return {"status": "INVALID_CREDIT_AMOUNT", "committed": False}
        with self._transaction():
            existing_usage = self.data["usage"].get(request_id)
            if isinstance(existing_usage, dict):
                if actual_amount is not None and existing_usage.get("amount") != float(actual_amount):
                    return {"status": "REQUEST_ID_CONFLICT", "committed": False}
                return {"status": "ALREADY_COMMITTED", "committed": False}
            reservation = self.data["reservations"].get(request_id)
            if not isinstance(reservation, dict):
                return {"status": "RESERVATION_NOT_FOUND", "committed": False}
            amount = float(reservation["amount"]) if actual_amount is None else float(actual_amount)
            record = {
                "provider_id": reservation["provider_id"],
                "amount": amount,
                "unit": reservation["unit"],
                "committed_at": self.clock().astimezone(timezone.utc).isoformat(),
                "snapshot_version": self.data["providers"].get(reservation["provider_id"], {}).get("snapshot_version", 1),
            }
            self.data["usage"][request_id] = record
            del self.data["reservations"][request_id]
            self._save()
            return {"status": "COMMITTED", "committed": True, "request_id": request_id, "amount": amount}

    def release(self, *, request_id: str, provider_id: str | None = None) -> dict[str, Any]:
        """Release only a reservation known not to have been consumed."""
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "released": False}
        with self._transaction():
            if request_id in self.data["usage"]:
                return {"status": "ALREADY_COMMITTED", "released": False}
            reservation = self.data["reservations"].get(request_id)
            if not isinstance(reservation, dict):
                return {"status": "RESERVATION_NOT_FOUND", "released": False}
            if provider_id is not None and reservation.get("provider_id") != provider_id:
                return {"status": "REQUEST_PROVIDER_MISMATCH", "released": False}
            del self.data["reservations"][request_id]
            self._save()
            return {"status": "RELEASED", "released": True, "request_id": request_id}

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
