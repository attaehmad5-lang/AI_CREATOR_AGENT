"""Verified, freshness-bounded provider/model request pricing.

Only explicitly verified USD-per-request price observations are eligible for routing.
This manager never scrapes provider websites or assumes an undocumented price.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

_ID_RE = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}\Z")


class ProviderPricingManager:
    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_age_seconds: int = 86400,
        clock: Any = None,
    ) -> None:
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not 1 <= max_age_seconds <= 31_536_000:
            raise ValueError("max_age_seconds must be within 1..31536000")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.max_age_seconds = max_age_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "prices": {}}
        self._reload()

    @staticmethod
    def _key(provider_id: str, model_id: str) -> str:
        return hashlib.sha256(f"{provider_id}\0{model_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def _aware_timestamp(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _valid_source(source_url: Any) -> bool:
        if (
            not isinstance(source_url, str) or not source_url or len(source_url) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in source_url)
        ):
            return False
        try:
            parsed = urlsplit(source_url)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme == "https" and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and (port is None or 1 <= port <= 65535)
        )

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "prices": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider pricing state JSON") from exc
        prices = loaded.get("prices") if isinstance(loaded, dict) else None
        if not isinstance(prices, dict):
            raise ValueError("invalid provider pricing state schema")
        for key, record in prices.items():
            if (
                not isinstance(key, str) or len(key) != 64
                or any(char not in "0123456789abcdef" for char in key)
                or not isinstance(record, dict)
                or not isinstance(record.get("provider_id"), str)
                or not _ID_RE.fullmatch(record["provider_id"])
                or not isinstance(record.get("model_id"), str)
                or not _ID_RE.fullmatch(record["model_id"])
                or record.get("unit") != "USD/request"
                or finite_number(record.get("cost_per_request"), minimum=0) is None
                or record.get("verified") is not True
                or not self._valid_source(record.get("source_url"))
                or self._aware_timestamp(record.get("observed_at")) is None
            ):
                raise ValueError("invalid provider pricing record")
            if key != self._key(record["provider_id"], record["model_id"]):
                raise ValueError("provider pricing key mismatch")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            with interprocess_file_lock(self.state_path.with_name(self.state_path.name + ".lock")):
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

    def update_price(
        self,
        *,
        provider_id: str,
        model_id: str,
        cost_per_request: float,
        source_url: str,
        verified: bool,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not isinstance(model_id, str) or not _ID_RE.fullmatch(model_id):
            return {"status": "INVALID_MODEL_ID", "saved": False}
        amount = finite_number(cost_per_request, minimum=0)
        if amount is None:
            return {"status": "INVALID_PRICE", "saved": False}
        if verified is not True:
            return {"status": "PRICE_VERIFICATION_REQUIRED", "saved": False}
        if not self._valid_source(source_url):
            return {"status": "INVALID_PRICE_SOURCE", "saved": False}
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return {"status": "INVALID_PRICING_CLOCK", "saved": False}
        observed = self._aware_timestamp(observed_at) if observed_at is not None else now.astimezone(timezone.utc)
        if observed is None:
            return {"status": "INVALID_PRICE_TIMESTAMP", "saved": False}
        now_utc = now.astimezone(timezone.utc)
        if observed > now_utc:
            return {"status": "FUTURE_PRICE_TIMESTAMP", "saved": False}
        key = self._key(provider_id, model_id)
        with self._transaction():
            existing = self.data["prices"].get(key)
            if isinstance(existing, dict):
                old_observed = self._aware_timestamp(existing.get("observed_at"))
                if old_observed is not None and observed <= old_observed:
                    return {"status": "PRICE_OBSERVATION_NOT_NEWER", "saved": False}
            self.data["prices"][key] = {
                "provider_id": provider_id,
                "model_id": model_id,
                "cost_per_request": amount,
                "unit": "USD/request",
                "source_url": source_url,
                "verified": True,
                "observed_at": observed.isoformat(),
                "updated_at": now_utc.isoformat(),
            }
            self._save()
        return {"status": "PRICE_UPDATED", "saved": True, "provider_id": provider_id, "model_id": model_id}

    def estimate(self, *, provider_id: str, model_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "available": False}
        if not isinstance(model_id, str) or not _ID_RE.fullmatch(model_id):
            return {"status": "INVALID_MODEL_ID", "available": False}
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return {"status": "INVALID_PRICING_CLOCK", "available": False}
        with self._transaction():
            record = self.data["prices"].get(self._key(provider_id, model_id))
            if not isinstance(record, dict):
                return {"status": "PRICE_NOT_FOUND", "available": False}
            observed = self._aware_timestamp(record.get("observed_at"))
            if observed is None:
                return {"status": "INVALID_PRICE_TIMESTAMP", "available": False}
            age = max(0.0, (now.astimezone(timezone.utc) - observed).total_seconds())
            if age > self.max_age_seconds:
                return {"status": "PRICE_STALE", "available": False, "age_seconds": age}
            return {
                "status": "PRICE_AVAILABLE",
                "available": True,
                "provider_id": provider_id,
                "model_id": model_id,
                "cost_per_request": float(record["cost_per_request"]),
                "unit": record["unit"],
                "source_url": record["source_url"],
                "observed_at": record["observed_at"],
                "age_seconds": age,
            }
