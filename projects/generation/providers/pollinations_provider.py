"""Authenticated Pollinations image-generation adapter.

The API key is read only from POLLINATIONS_API_KEY; it is never logged,
returned, or written to generated artifacts.
"""
import base64
import binascii
import os
from pathlib import Path

import requests


class PollinationsProviderError(RuntimeError):
    """Safe provider error with explicit retry/fallback semantics."""

    def __init__(self, message, *, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


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
                if response.status_code == 401:
                    raise PollinationsProviderError("Pollinations authentication failed (HTTP 401)", status_code=401, retryable=False)
                if response.status_code == 402:
                    raise PollinationsProviderError("Pollinations account or key budget is exhausted (HTTP 402)", status_code=402, retryable=False)
                raise PollinationsProviderError("Pollinations image request failed (HTTP {})".format(response.status_code), status_code=response.status_code, retryable=response.status_code in (408, 425, 429, 500, 502, 503, 504))

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
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(image_bytes)
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
