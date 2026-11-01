"""Strict, source-attributed provider pricing metadata and cost estimates.

Catalog prices are estimates only. They are not account balances, invoices,
quotes, or guarantees of provider billing.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .safe_numbers import finite_number


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\\Z")
_MODALITIES = {"image", "audio", "video", "text", "document"}
_UNITS = {"request", "image", "second", "minute", "1k_tokens", "megapixel", "audio_minute", "video_second"}
_SECRET_MARKERS = ("api_key", "token", "password", "secret", "credential", "authorization", "bearer")


class PricingCatalog:
    """Read-only pricing catalog with exact-match, date-aware estimation."""

    def __init__(self, path: str | Path) -> None:
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError("pricing catalog path is required")
        self.path = Path(path)
        self._prices: list[dict[str, Any]] = []
        self.reload()

    @staticmethod
    def _valid_timestamp(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None

    @staticmethod
    def _valid_date(value: Any, *, optional: bool = False) -> bool:
        if value is None and optional:
            return True
        if not isinstance(value, str):
            return False
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False

    @classmethod
    def _contains_secret_fields(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if any(marker in normalized for marker in _SECRET_MARKERS):
                    return True
                if cls._contains_secret_fields(child):
                    return True
        elif isinstance(value, list):
            return any(cls._contains_secret_fields(child) for child in value)
        return False

    @classmethod
    def _validate_record(cls, record: Any) -> bool:
        if not isinstance(record, dict) or cls._contains_secret_fields(record):
            return False
        required = {
            "price_id", "provider_id", "model_id", "modality", "pricing_unit",
            "unit_price", "currency", "source", "verified_at",
        }
        allowed = required | {"effective_from", "effective_to", "notes"}
        if not required.issubset(record) or set(record) - allowed:
            return False
        for key in ("price_id", "provider_id", "model_id"):
            if not isinstance(record.get(key), str) or not _ID.fullmatch(record[key]):
                return False
        if record["modality"] not in _MODALITIES or record["pricing_unit"] not in _UNITS:
            return False
        if finite_number(record["unit_price"], minimum=0) is None:
            return False
        if not isinstance(record["currency"], str) or not _CURRENCY.fullmatch(record["currency"]):
            return False
        if not isinstance(record["source"], str) or not record["source"].strip() or len(record["source"]) > 240:
            return False
        if any(marker in record["source"].casefold() for marker in _SECRET_MARKERS):
            return False
        if not cls._valid_timestamp(record["verified_at"]):
            return False
        start, end = record.get("effective_from"), record.get("effective_to")
        if not cls._valid_date(start, optional=True) or not cls._valid_date(end, optional=True):
            return False
        if start is not None and end is not None and date.fromisoformat(start) > date.fromisoformat(end):
            return False
        if "notes" in record and (
            not isinstance(record["notes"], str) or len(record["notes"]) > 1000
            or any(marker in record["notes"].casefold() for marker in _SECRET_MARKERS)
        ):
            return False
        return True

    def reload(self) -> dict[str, Any]:
        if not self.path.exists():
            self._prices = []
            return {"status": "PRICING_CATALOG_EMPTY", "count": 0}
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            self._prices = []
            return {"status": "PRICING_CATALOG_INVALID", "count": 0}
        if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("prices"), list):
            self._prices = []
            return {"status": "PRICING_CATALOG_INVALID", "count": 0}
        prices = data["prices"]
        if len(prices) > 100_000 or any(not self._validate_record(item) for item in prices):
            self._prices = []
            return {"status": "PRICING_CATALOG_INVALID", "count": 0}
        ids = [item["price_id"] for item in prices]
        if len(ids) != len(set(ids)):
            self._prices = []
            return {"status": "PRICING_CATALOG_INVALID", "count": 0}
        self._prices = [dict(item) for item in prices]
        return {"status": "PRICING_CATALOG_READY" if prices else "PRICING_CATALOG_EMPTY", "count": len(prices)}

    def list_prices(
        self, *, provider_id: str | None = None, model_id: str | None = None,
        modality: str | None = None,
    ) -> dict[str, Any]:
        for value in (provider_id, model_id):
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                return {"status": "INVALID_FILTER", "prices": [], "count": 0}
        if modality is not None and modality not in _MODALITIES:
            return {"status": "INVALID_FILTER", "prices": [], "count": 0}
        items = [
            dict(item) for item in self._prices
            if (provider_id is None or item["provider_id"] == provider_id)
            and (model_id is None or item["model_id"] == model_id)
            and (modality is None or item["modality"] == modality)
        ]
        items.sort(key=lambda item: (item["provider_id"], item["model_id"], item["price_id"]))
        return {"status": "PRICES_AVAILABLE" if items else "PRICING_NOT_FOUND", "prices": items, "count": len(items)}

    def estimate(
        self, *, provider_id: str, model_id: str, modality: str,
        units: float, currency: str, as_of: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(provider_id, str) or not _ID.fullmatch(provider_id)
            or not isinstance(model_id, str) or not _ID.fullmatch(model_id)
            or modality not in _MODALITIES
            or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
            or finite_number(units, minimum=0) is None or units <= 0
        ):
            return {"status": "INVALID_ESTIMATE_INPUT", "estimated": False}
        if as_of is None:
            effective_date = datetime.now(timezone.utc).date()
        elif self._valid_date(as_of):
            effective_date = date.fromisoformat(as_of)
        else:
            return {"status": "INVALID_ESTIMATE_DATE", "estimated": False}
        candidates = [
            item for item in self._prices
            if item["provider_id"] == provider_id
            and item["model_id"] == model_id
            and item["modality"] == modality
            and item["currency"] == currency
            and (item.get("effective_from") is None or date.fromisoformat(item["effective_from"]) <= effective_date)
            and (item.get("effective_to") is None or effective_date <= date.fromisoformat(item["effective_to"]))
        ]
        if not candidates:
            return {"status": "PRICING_NOT_FOUND", "estimated": False}
        if len(candidates) != 1:
            return {"status": "PRICING_AMBIGUOUS", "estimated": False}
        price = candidates[0]
        multiplier = 1000.0 if price["pricing_unit"] == "1k_tokens" else 1.0
        amount = finite_number(price["unit_price"], minimum=0)
        estimate = (amount * float(units)) / multiplier
        if finite_number(estimate, minimum=0) is None:
            return {"status": "ESTIMATE_OVERFLOW", "estimated": False}
        return {
            "status": "COST_ESTIMATED",
            "estimated": True,
            "estimated_cost": round(estimate, 10),
            "currency": currency,
            "provider_id": provider_id,
            "model_id": model_id,
            "modality": modality,
            "pricing_unit": price["pricing_unit"],
            "units": float(units),
            "unit_price": amount,
            "price_id": price["price_id"],
            "source": price["source"],
            "verified_at": price["verified_at"],
            "billing_authoritative": False,
        }
