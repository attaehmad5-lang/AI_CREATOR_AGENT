"""Verified, freshness-bounded model pricing from the Pollinations public catalog.

Only public catalog data is fetched. No API key is sent, no generation endpoint is
called, and missing/unsupported price components are never treated as free.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Callable


POLLINATIONS_MODELS_URL = "https://gen.pollinations.ai/v1/models"
MAX_CATALOG_BYTES = 2 * 1024 * 1024

# Current public catalog price fields and the request-usage units needed to estimate
# a request. Unknown non-currency fields fail closed rather than underestimating.
_PRICE_FIELDS = {
    "promptTextTokens": "prompt_text_tokens",
    "completionTextTokens": "completion_text_tokens",
    "promptCachedTokens": "prompt_cached_tokens",
    "promptCacheWriteTokens": "prompt_cache_write_tokens",
    "promptImageTokens": "prompt_images",
    "completionImageTokens": "completion_images",
    "promptVideoSeconds": "prompt_video_seconds",
    "completionVideoSeconds": "completion_video_seconds",
    "promptAudioSeconds": "prompt_audio_seconds",
    "completionAudioSeconds": "completion_audio_seconds",
    "promptAudioMinutes": "prompt_audio_minutes",
    "completionAudioMinutes": "completion_audio_minutes",
    "promptEmbeddingTokens": "prompt_embedding_tokens",
    "completionEmbeddingTokens": "completion_embedding_tokens",
}


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        amount = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0 or amount > 1e12:
        return None
    return amount


class ProviderModelPricingManager:
    """Refresh and estimate model-level prices from an official public catalog."""

    PROVIDER_ID = "pollinations"

    def __init__(
        self,
        *,
        max_age_seconds: int = 900,
        timeout_seconds: float = 8.0,
        clock: Callable[[], datetime] | None = None,
        fetcher: Callable[..., Any] | None = None,
    ) -> None:
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not 30 <= max_age_seconds <= 86400:
            raise ValueError("max_age_seconds must be within 30..86400")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 1 <= timeout_seconds <= 15:
            raise ValueError("timeout_seconds must be within 1..15")
        self.max_age_seconds = max_age_seconds
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fetcher = fetcher or self._http_fetch
        self._models: dict[str, dict[str, Any]] = {}
        self._aliases: dict[str, str] = {}
        self._observed_at: datetime | None = None

    @staticmethod
    def _http_fetch(url: str, *, timeout: float) -> Any:
        # The endpoint is fixed and public. No secret, account token or user input
        # is included in the request. Redirects are rejected to keep the trust
        # boundary on the official API host.
        if url != POLLINATIONS_MODELS_URL:
            raise ValueError("unsupported pricing catalog URL")
        import json
        import requests

        # Stream and cap the body before buffering it. Content-Length is only an
        # early rejection hint; the running byte counter is the authoritative cap.
        with requests.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
            headers={"Accept": "application/json"},
        ) as response:
            if 300 <= response.status_code < 400:
                raise ValueError("pricing catalog redirect rejected")
            response.raise_for_status()
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                if not declared_length.isdigit() or int(declared_length) > MAX_CATALOG_BYTES:
                    raise ValueError("pricing catalog exceeds size limit")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > MAX_CATALOG_BYTES:
                    raise ValueError("pricing catalog exceeds size limit")
        return json.loads(body)

    @staticmethod
    def _catalog_rows(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            rows = payload.get("data")
        else:
            rows = payload
        if not isinstance(rows, list) or not rows or len(rows) > 10000:
            raise ValueError("invalid pricing catalog")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("invalid pricing catalog record")
        return rows

    @staticmethod
    def _normalise_model(row: dict[str, Any], observed_at: datetime) -> dict[str, Any] | None:
        model_id = row.get("id")
        category = row.get("category")
        pricing = row.get("pricing")
        aliases = row.get("aliases", [])
        if (
            not isinstance(model_id, str)
            or not model_id
            or len(model_id) > 256
            or not isinstance(category, str)
            or category not in {"text", "image", "video", "audio", "embedding", "decision", "3d"}
            or not isinstance(pricing, dict)
            or not isinstance(aliases, list)
            or any(not isinstance(alias, str) or not alias or len(alias) > 256 for alias in aliases)
        ):
            return None
        currency = pricing.get("currency")
        paid_only = row["paidOnly"] if "paidOnly" in row else row.get("paid_only", False)
        if (
            not isinstance(currency, str) or not 1 <= len(currency) <= 16
            or not isinstance(paid_only, bool)
        ):
            return None
        rates: dict[str, float] = {}
        for key, value in pricing.items():
            if key == "currency":
                continue
            usage_key = _PRICE_FIELDS.get(key)
            amount = _amount(value)
            if usage_key is None or amount is None:
                # A new pricing field changes the calculation contract. Do not
                # silently ignore it and accidentally report an under-estimate.
                return None
            rates[usage_key] = amount
        if not rates:
            return None
        return {
            "id": model_id,
            "aliases": list(dict.fromkeys(aliases)),
            "category": category,
            "currency": currency.upper(),
            "rates": rates,
            "paid_only": paid_only,
            "source": POLLINATIONS_MODELS_URL,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        }

    def refresh_pollinations_catalog(self) -> dict[str, Any]:
        """Fetch the live public model catalog and atomically replace known prices."""
        try:
            payload = self.fetcher(POLLINATIONS_MODELS_URL, timeout=self.timeout_seconds)
            rows = self._catalog_rows(payload)
            observed_at = self.clock()
            if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
                return {"status": "INVALID_PRICING_CLOCK", "updated": False}
            observed_at = observed_at.astimezone(timezone.utc)
            models: dict[str, dict[str, Any]] = {}
            alias_targets: dict[str, set[str]] = {}
            for row in rows:
                if row.get("category") not in {"text", "image", "video", "audio", "embedding", "decision", "3d"}:
                    continue
                normalised = self._normalise_model(row, observed_at)
                if normalised is None:
                    continue
                model_id = normalised["id"]
                if model_id in models:
                    return {"status": "DUPLICATE_MODEL_ID_IN_CATALOG", "updated": False}
                models[model_id] = normalised
                alias_targets.setdefault(model_id.casefold(), set()).add(model_id)
                for alias in normalised["aliases"]:
                    alias_targets.setdefault(alias.casefold(), set()).add(model_id)
            # A lookup key resolves only when every occurrence points to one model.
            # Ambiguous aliases are omitted instead of selecting whichever row
            # happens to appear last in the upstream response.
            aliases = {
                key: next(iter(targets))
                for key, targets in alias_targets.items()
                if len(targets) == 1
            }
            if not models:
                return {"status": "NO_VERIFIED_MODEL_PRICES", "updated": False}
            self._models = models
            self._aliases = aliases
            self._observed_at = observed_at
            return {
                "status": "MODEL_PRICING_UPDATED",
                "updated": True,
                "model_count": len(models),
                "priced_model_count": len(models),
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source": POLLINATIONS_MODELS_URL,
            }
        except Exception as exc:
            # Return only a safe error type. Do not reflect URLs, response bodies,
            # provider details or any credential-like content.
            return {
                "status": "MODEL_PRICING_REFRESH_FAILED",
                "updated": False,
                "error_type": type(exc).__name__,
            }

    def _fresh(self) -> bool:
        if self._observed_at is None:
            return False
        try:
            now = self.clock()
        except Exception:
            return False
        if not isinstance(now, datetime) or now.tzinfo is None:
            return False
        age = (now.astimezone(timezone.utc) - self._observed_at).total_seconds()
        return 0 <= age <= self.max_age_seconds

    def get_model_record(
        self, provider_id: str, model_id: str, *, modality: str | None = None
    ) -> dict[str, Any]:
        if provider_id != self.PROVIDER_ID or not isinstance(model_id, str):
            return {"status": "MODEL_NOT_FOUND"}
        canonical = self._aliases.get(model_id.casefold())
        model = self._models.get(canonical) if canonical else None
        if not isinstance(model, dict):
            return {"status": "MODEL_NOT_FOUND"}
        if modality is not None and model.get("category") != modality:
            return {"status": "MODEL_MODALITY_MISMATCH"}
        if not self._fresh():
            return {"status": "MODEL_CATALOG_STALE"}
        return {
            "status": "MODEL_FOUND",
            "model": {
                "id": model["id"],
                "versions": [],
                "category": model["category"],
                "aliases": list(model["aliases"]),
            },
        }

    def estimate_request(
        self,
        *,
        provider_id: str,
        model_id: str,
        modality: str,
        usage: dict[str, Any],
        model_version: str | None = None,
    ) -> dict[str, Any]:
        if provider_id != self.PROVIDER_ID:
            return {"status": "MODEL_PRICE_NOT_SUPPORTED"}
        if not isinstance(model_id, str) or not model_id or not isinstance(modality, str):
            return {"status": "INVALID_MODEL_PRICE_REQUEST"}
        if not isinstance(usage, dict) or not usage:
            return {"status": "PRICING_USAGE_REQUIRED"}
        if any(key not in set(_PRICE_FIELDS.values()) for key in usage):
            return {"status": "INVALID_PRICING_USAGE"}
        if not self._fresh():
            return {"status": "MODEL_PRICING_STALE"}
        canonical = self._aliases.get(model_id.casefold())
        model = self._models.get(canonical) if canonical else None
        if not isinstance(model, dict):
            return {"status": "MODEL_PRICE_NOT_FOUND"}
        if model.get("category") != modality:
            return {"status": "MODEL_PRICE_MODALITY_MISMATCH"}
        if model_version is not None:
            return {"status": "MODEL_VERSION_PRICE_UNAVAILABLE"}
        total = 0.0
        components: dict[str, float] = {}
        for usage_key, rate in model["rates"].items():
            if rate == 0:
                components[usage_key] = 0.0
                continue
            quantity = _amount(usage.get(usage_key))
            if quantity is None:
                return {
                    "status": "PRICING_USAGE_INCOMPLETE",
                    "missing_component": usage_key,
                }
            charge = rate * quantity
            if not math.isfinite(charge) or charge > 1e12:
                return {"status": "INVALID_ESTIMATED_MODEL_PRICE"}
            components[usage_key] = round(charge, 12)
            total += charge
        if not math.isfinite(total) or total < 0 or total > 1e12:
            return {"status": "INVALID_ESTIMATED_MODEL_PRICE"}
        return {
            "status": "MODEL_PRICE_ESTIMATED",
            "provider_id": provider_id,
            "model_id": model["id"],
            "modality": model["category"],
            "estimated_cost": round(total, 12),
            "unit": model["currency"],
            "components": components,
            "paid_only": model["paid_only"],
            "source": model["source"],
            "observed_at": model["observed_at"],
        }
