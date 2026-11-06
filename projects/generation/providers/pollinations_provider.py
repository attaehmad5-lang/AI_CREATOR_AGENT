"""Authenticated Pollinations image-generation adapter.

The API key is read only from POLLINATIONS_API_KEY; it is never logged,
returned, or written to generated artifacts.
"""
import base64
import binascii
import json as jsonlib
import math
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.http_probe import ProviderPinnedHTTPSOpener
from .atomic_output import atomic_write_bytes


_POLLINATIONS_ERROR_CODES = {
    "insufficient_balance",
    "key_budget_exhausted",
    "quest_pollen_only",
    "output_token_limit_exceeded",
}


class PollinationsProviderError(RuntimeError):
    """Safe provider error with explicit retry/fallback semantics."""

    def __init__(
        self, message, *, status_code=None, retryable=False,
        provider_error_code=None, retry_after_seconds=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider_error_code = provider_error_code
        self.retry_after_seconds = retry_after_seconds


class _PinnedPollinationsResponse:
    """Small requests-compatible response wrapper with an enforced JSON body cap."""

    def __init__(self, response, max_response_bytes):
        self._response = response
        self.status_code = response.getcode()
        self.headers = getattr(response, "headers", {})
        self.max_response_bytes = max_response_bytes
        self._payload = None
        self._loaded = False

    def json(self):
        if not self._loaded:
            body = self._response.read(self.max_response_bytes + 1)
            if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
                raise ValueError("Pollinations response exceeds the size limit")
            self._payload = jsonlib.loads(body.decode("utf-8"))
            self._loaded = True
        return self._payload

    def close(self):
        self._response.close()


class _PinnedPollinationsSession:
    """Use the shared DNS-pinned HTTPS transport for the fixed Pollinations API."""

    def __init__(self, *, max_response_bytes, resolver=None):
        self.max_response_bytes = max_response_bytes
        self.opener = ProviderPinnedHTTPSOpener(resolver=resolver)

    def post(self, url, *, headers, json, timeout):
        if url != PollinationsProvider.endpoint:
            raise ValueError("unsupported Pollinations generation endpoint")
        validation = ConnectionManager.validate_endpoint(
            url, allowed_hosts={"gen.pollinations.ai"}
        )
        if validation.get("valid") is not True:
            raise ValueError("unsafe Pollinations generation endpoint")
        body = jsonlib.dumps(
            json, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request_headers = dict(headers)
        request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=request_headers, method="POST")
        response = self.opener.open(request, timeout=timeout)
        return _PinnedPollinationsResponse(response, self.max_response_bytes)


class PollinationsProvider:
    name = "Pollinations"
    endpoint = "https://gen.pollinations.ai/v1/images/generations"
    default_model = "black-forest-labs/flux.1-schnell"
    max_response_bytes = 20 * 1024 * 1024

    def __init__(self, session=None, environ=None, timeout=180, *, resolver=None):
        if session is not None and resolver is not None:
            raise ValueError("custom session and DNS resolver cannot be combined")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("timeout must be a finite positive number") from None
        if not math.isfinite(timeout_value) or not 0.1 <= timeout_value <= 300:
            raise ValueError("timeout must be between 0.1 and 300 seconds")
        self.environ = os.environ if environ is None else environ
        if not isinstance(self.environ, dict) and not hasattr(self.environ, "get"):
            raise ValueError("environment must be a mapping")
        self.timeout = timeout_value
        self.session = session if session is not None else _PinnedPollinationsSession(
            max_response_bytes=(self.max_response_bytes * 4 // 3) + 1024 * 1024,
            resolver=resolver,
        )

    @staticmethod
    def _image_extension(image_bytes):
        if image_bytes.startswith(bytes.fromhex("89504e470d0a1a0a")):
            return ".png"
        if image_bytes.startswith(bytes.fromhex("ffd8ff")):
            return ".jpg"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return ".webp"
        raise RuntimeError("Pollinations response is not a supported image")

    @staticmethod
    def _safe_provider_error_code(response):
        """Return only documented Pollinations codes, never arbitrary response text."""
        try:
            payload = response.json()
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if isinstance(error, dict):
            candidate = error.get("code")
        elif isinstance(error, str):
            candidate = error
        else:
            candidate = payload.get("code")
        if not isinstance(candidate, str) or len(candidate) > 80:
            return None
        normalized = candidate.strip().casefold().replace("-", "_").replace(" ", "_")
        if not re.fullmatch(r"[a-z0-9_.]+", normalized):
            return None
        return normalized if normalized in _POLLINATIONS_ERROR_CODES else None

    @staticmethod
    def _safe_retry_after_seconds(response):
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        if not callable(getter):
            return None
        value = getter("Retry-After")
        if value is None:
            value = getter("retry-after")
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                retry_at = parsedate_to_datetime(value) if isinstance(value, str) else None
                if retry_at is None:
                    return None
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError, IndexError):
                return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, 300.0)

    def generate_image(self, prompt, output_path):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Image prompt must be a non-empty string")
        api_key = self.environ.get("POLLINATIONS_API_KEY", "")
        if (
            not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 16384
            or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
        ):
            raise RuntimeError("POLLINATIONS_API_KEY is missing or invalid")
        api_key = api_key.strip()

        model = self.environ.get("POLLINATIONS_IMAGE_MODEL", self.default_model).strip()
        if not model:
            raise RuntimeError("POLLINATIONS_IMAGE_MODEL is empty")

        response = self.session.post(
            self.endpoint,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": model,
                "prompt": prompt.strip(),
                "size": "1024x1024",
                "n": 1,
                "response_format": "b64_json",
            },
            timeout=self.timeout,
        )
        try:
            if response.status_code != 200:
                # Do not include response bodies: providers may echo sensitive data.
                # Only parse exact, documented structured codes and bounded Retry-After.
                provider_error_code = (
                    self._safe_provider_error_code(response)
                    if response.status_code in (400, 402) else None
                )
                retry_after_seconds = (
                    self._safe_retry_after_seconds(response)
                    if response.status_code == 429 else None
                )
                if response.status_code == 401:
                    message = "Pollinations authentication failed (HTTP 401)"
                elif response.status_code == 402:
                    message = "Pollinations billing or balance requirement not met (HTTP 402)"
                else:
                    message = "Pollinations image request failed (HTTP {})".format(response.status_code)
                raise PollinationsProviderError(
                    message,
                    status_code=response.status_code,
                    retryable=response.status_code in (408, 425, 429, 500, 502, 503, 504),
                    provider_error_code=provider_error_code,
                    retry_after_seconds=retry_after_seconds,
                )

            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise RuntimeError("Pollinations returned invalid JSON") from None

            data = payload.get("data") if isinstance(payload, dict) else None
            item = data[0] if isinstance(data, list) and data else None
            encoded = item.get("b64_json") if isinstance(item, dict) else None
            if not isinstance(encoded, str) or not encoded:
                raise RuntimeError("Pollinations response did not contain image data")
            try:
                image_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise RuntimeError("Pollinations returned invalid base64 image data") from None

            if not image_bytes or len(image_bytes) > self.max_response_bytes:
                raise RuntimeError("Pollinations image is empty or exceeds the size limit")
            extension = self._image_extension(image_bytes)

            destination = Path(output_path)
            if destination.suffix.lower() != extension:
                destination = destination.with_suffix(extension)
            atomic_write_bytes(destination, image_bytes, max_bytes=self.max_response_bytes)
            return {
                "status": "COMPLETED",
                "provider": self.name,
                "model": model,
                "output": str(destination),
                "format": extension.lstrip("."),
                "bytes": len(image_bytes),
            }
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
