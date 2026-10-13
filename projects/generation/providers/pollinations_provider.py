"""Authenticated, bounded Pollinations image-generation adapter.

Uses the documented gen.pollinations.ai image endpoint. API keys are read from
POLLINATIONS_API_KEY (or an explicitly injected environment mapping), never from
URLs, prompts, output metadata, or source code.
"""
from __future__ import annotations

import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

from projects.provider_store.http_probe import ProviderHTTPStatusError


class PollinationsProvider:
    name = "Pollinations"
    BASE_URL = "https://gen.pollinations.ai/image/"

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        timeout_seconds: float = 180.0,
        max_output_bytes: int = 30 * 1024 * 1024,
    ) -> None:
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("environ must be a mapping")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 300
        ):
            raise ValueError("timeout_seconds must be between 0 and 300")
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 1024 <= max_output_bytes <= 100 * 1024 * 1024
        ):
            raise ValueError("max_output_bytes must be between 1024 and 104857600")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide an open method")
        self.environ = os.environ if environ is None else environ
        self.opener = opener or build_opener()
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def _valid_model(model: Any) -> bool:
        return isinstance(model, str) and bool(
            re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", model)
        ) and not model.startswith("/") and ".." not in model.split("/")

    @staticmethod
    def _valid_image_signature(data: bytes, content_type: str) -> bool:
        media_type = content_type.split(";", 1)[0].strip().casefold()
        if media_type == "image/png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if media_type in {"image/jpeg", "image/jpg"}:
            return data.startswith(b"\xff\xd8\xff")
        if media_type == "image/webp":
            return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        if media_type == "image/avif":
            return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}
        return False

    def generate_image(
        self,
        prompt: str,
        output_path: str | Path,
        *,
        model: str = "flux",
        width: int = 1024,
        height: int = 1024,
        seed: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise ValueError("prompt must contain 1 to 20000 characters")
        if any(ord(char) < 32 and char not in "\n\r\t" for char in prompt):
            raise ValueError("prompt contains unsupported control characters")
        if not self._valid_model(model):
            raise ValueError("model identifier is invalid")
        for name, value in (("width", width), ("height", height)):
            if not isinstance(value, int) or isinstance(value, bool) or not 256 <= value <= 2048:
                raise ValueError(f"{name} must be an integer between 256 and 2048")
        if seed is not None and (
            not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**32 - 1
        ):
            raise ValueError("seed must be an unsigned 32-bit integer")

        api_key = self.environ.get("POLLINATIONS_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise PermissionError("POLLINATIONS_API_KEY is missing")
        if api_key != api_key.strip() or len(api_key) > 16384 or any(
            ord(char) < 32 or ord(char) == 127 for char in api_key
        ):
            raise PermissionError("POLLINATIONS_API_KEY format is invalid")

        params: dict[str, str | int] = {"model": model, "width": width, "height": height}
        if seed is not None:
            params["seed"] = seed
        url = self.BASE_URL + quote(prompt.strip(), safe="") + "?" + urlencode(params)
        if len(url) > 8192:
            raise ValueError("prompt and parameters exceed the safe URL length limit")
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "image/png, image/jpeg, image/webp, image/avif",
                "User-Agent": "AI_CREATOR_AGENT-Pollinations/1.0",
            },
            method="GET",
        )
        try:
            response = self.opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            status = exc.code
            retry_after = None
            try:
                raw = exc.headers.get("Retry-After") if exc.headers else None
                if isinstance(raw, str) and len(raw) <= 128 and not any(
                    ord(char) < 32 or ord(char) == 127 for char in raw
                ):
                    retry_after = raw
            except Exception:
                retry_after = None
            exc.close()
            raise ProviderHTTPStatusError(status, retry_after) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                raise TimeoutError("Pollinations image generation timed out") from None
            raise ConnectionError("Pollinations image service is unreachable") from None

        try:
            status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ConnectionError("Pollinations did not return a successful HTTP status")
            headers = getattr(response, "headers", None)
            content_type = headers.get("Content-Type", "") if headers is not None else ""
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, self.max_output_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_output_bytes:
                    raise ValueError("Pollinations image exceeded the configured size limit")
                chunks.append(chunk)
            image_bytes = b"".join(chunks)
            if not self._valid_image_signature(image_bytes, content_type):
                raise ValueError("Pollinations response was not a supported image artifact")

            target = Path(output_path)
            allowed_extensions = {
                "image/png": {".png"},
                "image/jpeg": {".jpg", ".jpeg"},
                "image/jpg": {".jpg", ".jpeg"},
                "image/webp": {".webp"},
                "image/avif": {".avif"},
            }
            media_type = content_type.split(";", 1)[0].strip().casefold()
            if target.suffix.casefold() not in allowed_extensions.get(media_type, set()):
                raise ValueError("output extension does not match image content type")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=".pollinations-", suffix=".tmp",
                    dir=str(target.parent), delete=False,
                ) as temporary:
                    temporary.write(image_bytes)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = temporary.name
                os.replace(temporary_path, target)
                temporary_path = None
            finally:
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass
            return {
                "status": "COMPLETED",
                "provider": self.name,
                "model": model,
                "output": str(target),
                "content_type": content_type.split(";", 1)[0].strip().casefold(),
                "bytes_written": len(image_bytes),
            }
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
