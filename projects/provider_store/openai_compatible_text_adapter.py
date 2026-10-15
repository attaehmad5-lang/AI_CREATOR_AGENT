"""Bounded OpenAI-compatible text adapter, including safe loopback-only local endpoints.

No request/response body, credential, or exception text is logged or returned.
The adapter is usable as a process-local provider plugin and does not require a cloud
account when pointed at a local OpenAI-compatible server.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connection_manager import ConnectionManager


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class OpenAICompatibleTextAdapter:
    API_VERSION = "1.0"
    adapter_api_version = "1.0"

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        allowed_hosts: list[str] | None = None,
        api_key_env: str | None = None,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        self.endpoint = self._validate_endpoint(endpoint, allowed_hosts)
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", model):
            raise ValueError("model identifier is invalid")
        if api_key_env is not None and (
            not isinstance(api_key_env, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", api_key_env)
        ):
            raise ValueError("API key environment variable name is invalid")
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("environment mapping is invalid")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 300
        ):
            raise ValueError("timeout must be between 0 and 300 seconds")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or not 1 <= max_response_bytes <= 20_000_000
        ):
            raise ValueError("response size limit is invalid")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide an open method")
        self.model = model
        self.api_key_env = api_key_env
        self.environ = os.environ if environ is None else environ
        self.opener = opener or build_opener(_NoRedirect())
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes

    @staticmethod
    def _validate_endpoint(endpoint: str, allowed_hosts: list[str] | None) -> str:
        if (
            not isinstance(endpoint, str) or len(endpoint) > 2048
            or endpoint != endpoint.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in endpoint)
        ):
            raise ValueError("endpoint is invalid")
        parsed = urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("endpoint credentials and fragments are forbidden")
        if not parsed.hostname or parsed.query:
            raise ValueError("endpoint host or query is invalid")
        if parsed.scheme == "https":
            validation = ConnectionManager.validate_endpoint(
                endpoint.strip(), allowed_hosts=allowed_hosts
            )
            if not validation.get("valid"):
                raise ValueError("HTTPS endpoint failed host policy")
        elif parsed.scheme == "http":
            host = parsed.hostname.casefold()
            loopback = host == "localhost"
            if not loopback:
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    loopback = False
            if not loopback:
                raise ValueError("plain HTTP is allowed only for loopback local servers")
        else:
            raise ValueError("endpoint must use HTTPS or loopback HTTP")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("endpoint port is invalid")
        if not parsed.path or parsed.path == "/":
            raise ValueError("full chat-completions endpoint path is required")
        return endpoint.strip()

    def generate_text(
        self,
        prompt: str,
        *,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        system_prompt: str | None = None,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200_000:
            raise ValueError("prompt must be a non-empty string within the size limit")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or len(system_prompt) > 50_000
        ):
            raise ValueError("system prompt is invalid")
        if (
            not isinstance(temperature, (int, float))
            or isinstance(temperature, bool)
            or not math.isfinite(temperature)
            or not 0 <= temperature <= 2
        ):
            raise ValueError("temperature must be between 0 and 2")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= 32768
        ):
            raise ValueError("max_tokens must be between 1 and 32768")

        messages = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": max_tokens,
            "stream": False,
        }, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key_env is not None:
            token = self.environ.get(self.api_key_env)
            if not isinstance(token, str) or not token.strip():
                raise PermissionError("configured API credential is missing")
            headers["Authorization"] = "Bearer " + token
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            response = self.opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PermissionError("text provider rejected credentials or permissions") from None
            if exc.code == 429:
                raise ConnectionError("text provider rate limit exceeded") from None
            if 300 <= exc.code < 400:
                raise ConnectionError("text provider redirect was rejected") from None
            raise ConnectionError(f"text provider returned HTTP {exc.code}") from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                raise TimeoutError("text provider request timed out") from None
            raise ConnectionError("text provider is unreachable") from None

        try:
            status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ConnectionError("text provider did not return a successful status")
            raw = response.read(self.max_response_bytes + 1)
            if not isinstance(raw, bytes) or len(raw) > self.max_response_bytes:
                raise ValueError("text provider response exceeded size limit")
            try:
                payload = json.loads(
                    raw.decode("utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError("invalid JSON constant")),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
                raise ValueError("text provider returned invalid JSON") from None
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("text provider response schema is invalid")
            message = choices[0].get("message")
            text = message.get("content") if isinstance(message, dict) else None
            if not isinstance(text, str):
                raise ValueError("text provider response content is invalid")
            return text
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
