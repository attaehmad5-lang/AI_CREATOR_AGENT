"""Safe provider model/capability metadata refresh.

Only explicitly configured, allowlisted HTTPS metadata endpoints are fetched.
The raw response is never persisted or returned; only a small validated schema is
written back through ConnectionManager's catalog transaction.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connection_manager import ConnectionManager
from .safe_numbers import finite_number


class _RejectMetadataRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_MODALITY_CAPABILITIES = {
    "image_generation", "image_editing", "image_upscaling", "video_generation",
    "speech_generation", "transcription", "text_generation",
}

_CAPABILITY_ALIASES = {
    "text-to-image": "image_generation",
    "image-generation": "image_generation",
    "image_generation": "image_generation",
    "image-to-image": "image_editing",
    "image_editing": "image_editing",
    "image-upscaling": "image_upscaling",
    "text-to-video": "video_generation",
    "video_generation": "video_generation",
    "text-to-speech": "speech_generation",
    "speech_generation": "speech_generation",
    "automatic-speech-recognition": "transcription",
    "speech-to-text": "transcription",
    "transcription": "transcription",
    "text-generation": "text_generation",
    "conversational": "text_generation",
    "text_generation": "text_generation",
    "image": "image_generation",
    "video": "video_generation",
    "audio": "speech_generation",
    "text": "text_generation",
}


class ProviderMetadataManager:
    """Refresh model metadata using a configured endpoint and a trusted JSON fetcher."""

    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        clock: Callable[[], datetime] | None = None,
        max_response_bytes: int = 2_000_000,
        max_models: int = 500,
    ) -> None:
        if not callable(getattr(connection_manager, "get_provider", None)) or not callable(
            getattr(connection_manager, "register_provider", None)
        ):
            raise ValueError("connection_manager must support provider lookup and catalog registration")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or not 1024 <= max_response_bytes <= 20_000_000
        ):
            raise ValueError("max_response_bytes must be between 1024 and 20000000")
        if not isinstance(max_models, int) or isinstance(max_models, bool) or not 1 <= max_models <= 5000:
            raise ValueError("max_models must be between 1 and 5000")
        self.connection_manager = connection_manager
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_response_bytes = max_response_bytes
        self.max_models = max_models

    @staticmethod
    def _endpoint(provider: dict[str, Any]) -> tuple[str | None, dict[str, str], str | None]:
        endpoint = provider.get("metadata_endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            return None, {}, "METADATA_SOURCE_NOT_CONFIGURED"
        hosts = provider.get("allowed_hosts")
        if not isinstance(hosts, list) or not hosts:
            return None, {}, "METADATA_HOST_ALLOWLIST_REQUIRED"
        validation = ConnectionManager.validate_endpoint(endpoint, allowed_hosts=hosts)
        if validation.get("valid") is not True:
            return None, {}, str(validation.get("status", "INVALID_METADATA_ENDPOINT"))
        query = provider.get("metadata_query", {})
        if not isinstance(query, dict) or len(query) > 20:
            return None, {}, "INVALID_METADATA_QUERY"
        normalized_query: dict[str, str] = {}
        for key, value in query.items():
            if (
                not isinstance(key, str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                or any(part in key.casefold() for part in ("token", "key", "secret", "password", "credential"))
                or not isinstance(value, (str, int))
                or isinstance(value, bool)
                or not str(value).strip()
                or len(str(value)) > 200
                or any(ord(char) < 32 or ord(char) == 127 for char in str(value))
                or any(marker in str(value).casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer "))
            ):
                return None, {}, "INVALID_METADATA_QUERY"
            normalized_query[key] = str(value)
        parsed = urlsplit(endpoint)
        if parsed.query or parsed.fragment:
            return None, {}, "INVALID_METADATA_ENDPOINT"
        return endpoint, normalized_query, None

    def refresh(
        self,
        provider_id: str,
        fetcher: Callable[[str, dict[str, str], float], Any],
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Refresh through an injected trusted fetcher, useful for provider-specific adapters."""
        timeout = finite_number(timeout_seconds, minimum=0)
        if timeout is None or timeout <= 0 or timeout > 120:
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "refreshed": False}
        if not callable(fetcher):
            return {"status": "INVALID_FETCHER", "provider": provider_id, "refreshed": False}
        provider = self.connection_manager.get_provider(provider_id)
        if not isinstance(provider, dict):
            return {"status": "NOT_FOUND", "provider": provider_id, "refreshed": False}
        endpoint, query, endpoint_error = self._endpoint(provider)
        if endpoint_error:
            return {"status": endpoint_error, "provider": provider_id, "refreshed": False}
        try:
            payload = fetcher(endpoint, dict(query), timeout)
        except Exception as exc:
            return {
                "status": "METADATA_FETCH_FAILED",
                "provider": provider_id,
                "refreshed": False,
                "error_type": type(exc).__name__,
            }
        normalized, error = self._normalize_payload(payload)
        if error:
            return {"status": error, "provider": provider_id, "refreshed": False}

        try:
            observed = self.clock()
        except Exception:
            return {"status": "METADATA_CLOCK_FAILED", "provider": provider_id, "refreshed": False}
        if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
            return {"status": "INVALID_METADATA_TIMESTAMP", "provider": provider_id, "refreshed": False}
        observed = observed.astimezone(timezone.utc).replace(microsecond=0)
        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
        revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        capabilities = sorted({
            capability
            for model in normalized
            for capability in model.get("capabilities", [])
            if capability in _MODALITY_CAPABILITIES
        })
        updates: dict[str, Any] = {
            "models": normalized,
            "metadata_source": endpoint,
            "metadata_observed_at": observed.isoformat(),
            "metadata_revision": revision,
            "metadata_status": "VERIFIED",
        }
        if capabilities:
            updates["capabilities"] = capabilities

        try:
            result = self.connection_manager.update_provider_metadata(provider_id, updates)
        except Exception as exc:
            return {
                "status": "METADATA_PERSISTENCE_FAILED",
                "provider": provider_id,
                "refreshed": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or result.get("saved") is not True:
            status = result.get("status", "METADATA_PERSISTENCE_FAILED") if isinstance(result, dict) else "METADATA_PERSISTENCE_FAILED"
            return {"status": status, "provider": provider_id, "refreshed": False}
        return {
            "status": "METADATA_REFRESHED",
            "provider": provider_id,
            "refreshed": True,
            "model_count": len(normalized),
            "capability_count": len(capabilities),
            "revision": revision,
            "observed_at": observed.isoformat(),
        }

    def refresh_http(
        self,
        provider_id: str,
        *,
        timeout_seconds: float = 5.0,
        opener: Any = None,
    ) -> dict[str, Any]:
        """Fetch a configured public JSON metadata endpoint with bounded HTTPS transport."""
        timeout = finite_number(timeout_seconds, minimum=0)
        if timeout is None or timeout <= 0 or timeout > 120:
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "refreshed": False}
        if opener is not None and not callable(getattr(opener, "open", None)):
            return {"status": "INVALID_OPENER", "provider": provider_id, "refreshed": False}
        provider = self.connection_manager.get_provider(provider_id)
        if not isinstance(provider, dict):
            return {"status": "NOT_FOUND", "provider": provider_id, "refreshed": False}
        endpoint, query, endpoint_error = self._endpoint(provider)
        if endpoint_error:
            return {"status": endpoint_error, "provider": provider_id, "refreshed": False}
        transport = opener or build_opener(_RejectMetadataRedirects())

        def fetch(url: str, params: dict[str, str], timeout: float) -> Any:
            parsed = urlsplit(url)
            final_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
            request = Request(final_url, headers={
                "Accept": "application/json",
                "User-Agent": "AI_CREATOR_AGENT-MetadataRefresh/1.0",
            }, method="GET")
            try:
                response = transport.open(request, timeout=timeout)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                raise ConnectionError(f"metadata endpoint returned HTTP {code}") from None
            except URLError as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                    raise TimeoutError("metadata endpoint timed out") from None
                raise ConnectionError("metadata endpoint is unreachable") from None
            try:
                status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise ConnectionError("metadata endpoint did not return HTTP 2xx")
                content_length = None
                headers = getattr(response, "headers", None)
                if headers is not None:
                    raw_length = headers.get("Content-Length")
                    if raw_length is not None:
                        try:
                            content_length = int(raw_length)
                        except (TypeError, ValueError):
                            raise ValueError("invalid metadata content length") from None
                if content_length is not None and (
                    content_length < 0 or content_length > self.max_response_bytes
                ):
                    raise ValueError("metadata response exceeds size limit")
                body = response.read(self.max_response_bytes + 1)
                if not isinstance(body, (bytes, bytearray)) or len(body) > self.max_response_bytes:
                    raise ValueError("metadata response exceeds size limit")
                return json.loads(bytes(body).decode("utf-8"))
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        return self.refresh(provider_id, fetch, timeout_seconds=timeout)

    def _normalize_payload(self, payload: Any) -> tuple[list[dict[str, Any]], str | None]:
        if isinstance(payload, dict):
            models = payload.get("models")
            if models is None:
                models = payload.get("data")
            if models is None:
                models = payload.get("items")
        else:
            models = payload
        if not isinstance(models, list) or not models:
            return [], "INVALID_METADATA_PAYLOAD"
        if len(models) > self.max_models:
            return [], "METADATA_MODEL_LIMIT_EXCEEDED"

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                return [], "INVALID_METADATA_MODEL"
            raw_id = item.get("id") or item.get("model_id") or item.get("model") or item.get("name")
            if (
                not isinstance(raw_id, str)
                or not raw_id.strip()
                or len(raw_id) > 300
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_id)
                or any(marker in raw_id.casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer "))
            ):
                return [], "INVALID_METADATA_MODEL_ID"
            model_id = raw_id.strip()
            if model_id in seen:
                return [], "DUPLICATE_METADATA_MODEL_ID"
            seen.add(model_id)

            raw_capabilities = item.get("capabilities")
            if raw_capabilities is None:
                raw_capabilities = item.get("tasks")
            if raw_capabilities is None and isinstance(item.get("pipeline_tag"), str):
                raw_capabilities = [item["pipeline_tag"]]
            if raw_capabilities is None and isinstance(item.get("output_modalities"), list):
                raw_capabilities = item["output_modalities"]
            if raw_capabilities is None:
                raw_capabilities = []
            if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 50:
                return [], "INVALID_MODEL_CAPABILITIES"
            capabilities: set[str] = set()
            for raw_capability in raw_capabilities:
                if not isinstance(raw_capability, str):
                    return [], "INVALID_MODEL_CAPABILITIES"
                capability = _CAPABILITY_ALIASES.get(raw_capability.strip().casefold())
                if capability is None:
                    # Keep safe provider-specific traits on the model record, but never
                    # infer provider-wide modalities from unknown feature labels.
                    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", raw_capability):
                        capability = raw_capability
                    elif re.fullmatch(r"[a-z][a-z0-9-]{0,79}", raw_capability):
                        continue
                    else:
                        return [], "INVALID_MODEL_CAPABILITIES"
                capabilities.add(capability)

            raw_version = item.get("version")
            if raw_version is None:
                raw_version = item.get("revision")
            if raw_version is not None and (
                not isinstance(raw_version, str)
                or not raw_version.strip()
                or len(raw_version) > 120
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_version)
                or any(marker in raw_version.casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer "))
            ):
                return [], "INVALID_MODEL_VERSION"

            raw_pricing = item.get("pricing")
            if raw_pricing is None and "price" in item:
                raw_pricing = {"price": item["price"]}
            pricing: dict[str, float] = {}
            pricing_descriptors: dict[str, str] = {}
            if raw_pricing is not None:
                if not isinstance(raw_pricing, dict) or len(raw_pricing) > 20:
                    return [], "INVALID_MODEL_PRICING"
                descriptor_keys = {
                    "unit": "pricing_unit",
                    "type": "pricing_type",
                    "currency": "pricing_currency",
                    "description": "pricing_description",
                }
                for key, value in raw_pricing.items():
                    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
                        return [], "INVALID_MODEL_PRICING"
                    if key in descriptor_keys:
                        if (
                            not isinstance(value, str) or not value.strip() or len(value) > 120
                            or any(ord(char) < 32 or ord(char) == 127 for char in value)
                            or any(marker in value.casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer "))
                        ):
                            return [], "INVALID_MODEL_PRICING"
                        pricing_descriptors[descriptor_keys[key]] = value.strip()
                        continue
                    amount = finite_number(value, minimum=0)
                    if amount is None:
                        return [], "INVALID_MODEL_PRICING"
                    pricing[key] = amount

            record: dict[str, Any] = {"id": model_id}
            if raw_version is not None:
                record["version"] = raw_version.strip()
            if capabilities:
                record["capabilities"] = sorted(capabilities)
            if pricing:
                record["pricing"] = pricing
            record.update(pricing_descriptors)
            normalized.append(record)

        normalized.sort(key=lambda record: record["id"].casefold())
        return normalized, None
