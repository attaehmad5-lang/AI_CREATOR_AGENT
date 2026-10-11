"""Fresh, verified provider free-credit snapshots supplied by provider-specific probes."""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


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
        self.data: dict[str, Any] = {"version": "1.0", "providers": {}}
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("invalid provider credit state JSON") from exc
            if not isinstance(loaded, dict) or not isinstance(loaded.get("providers"), dict):
                raise ValueError("invalid provider credit state schema")
            for provider_id, record in loaded["providers"].items():
                if (
                    not isinstance(provider_id, str)
                    or not isinstance(record, dict)
                    or not self._valid_amount(record.get("free_credits_remaining"))
                    or not isinstance(record.get("verified"), bool)
                ):
                    raise ValueError("invalid provider credit snapshot")
                self._parse_timestamp(record.get("observed_at"))
            self.data = loaded

    @staticmethod
    def _valid_amount(value: Any) -> bool:
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
        with self._lock:
            self.data["providers"][provider_id] = record
            self._save()
        return {"status": "CREDIT_BALANCE_UPDATED", "provider": provider_id, "verified": True, "observed_at": record["observed_at"]}

    def get_balance(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "verified": False}
        record = self.data["providers"].get(provider_id)
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

    def usable_free_credits(self) -> dict[str, float]:
        """Return only fresh verified balances; stale or unknown balances never authorize free routing."""
        result: dict[str, float] = {}
        with self._lock:
            provider_ids = list(self.data.get("providers", {}).keys())
        for provider_id in provider_ids:
            balance = self.get_balance(provider_id)
            if balance.get("status") == "CREDIT_BALANCE_FRESH" and balance.get("verified") is True:
                result[provider_id] = float(balance["free_credits_remaining"])
        return result

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
