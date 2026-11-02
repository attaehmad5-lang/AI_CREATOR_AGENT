"""Authenticated Pollinations image-generation adapter.

The API key is read only from POLLINATIONS_API_KEY; it is never logged,
returned, or written to generated artifacts.
"""
import base64
import binascii
import math
import os
import re
from pathlib import Path

import requests

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


class PollinationsProvider:
    name = "Pollinations"
    endpoint = "https://gen.pollinations.ai/v1/images/generations"
    default_model = "black-forest-labs/flux.1-schnell"
    max_response_bytes = 20 * 1024 * 1024

    def __init__(self, session=None, environ=None, timeout=180):
        self.session = session or requests
        self.environ = os.environ if environ is None else environ
        self.timeout = timeout

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
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, 300.0)

    def generate_image(self, prompt, output_path):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Image prompt must be a non-empty string")
        api_key = self.environ.get("POLLINATIONS_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("POLLINATIONS_API_KEY is not configured")

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
