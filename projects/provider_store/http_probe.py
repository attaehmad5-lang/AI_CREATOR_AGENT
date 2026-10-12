"""Safe generic HTTPS connectivity probe for explicitly configured provider endpoints.

This probe checks transport/authentication reachability only. It does not claim that
an endpoint supports generation or that a provider has available credits.
"""
from __future__ import annotations

import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connection_manager import ConnectionManager


class ProviderHTTPAuthError(PermissionError):
    """Authentication failure carrying a safe HTTP status code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__("provider rejected credentials or permissions")


class ProviderHTTPStatusError(ConnectionError):
    """Safe provider HTTP failure with bounded status metadata for retry decisions."""

    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"provider returned HTTP {status_code}")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Redirect targets are never followed automatically; callers must configure
        # the final endpoint explicitly and pass the host allowlist again.
        return None


class ProviderHTTPConnectivityProbe:
    """Perform a bounded HTTPS GET against a validated, explicitly configured endpoint."""

    _DEFAULT_TOKEN_ENV = {
        "huggingface": "HF_TOKEN",
        "replicate": "REPLICATE_API_TOKEN",
        "openai_image": "OPENAI_API_KEY",
        "openai_voice": "OPENAI_API_KEY",
        "openai_sora": "OPENAI_API_KEY",
        "whisper": "OPENAI_API_KEY",
        "google_image": "GOOGLE_API_KEY",
        "google_speech": "GOOGLE_API_KEY",
        "google_lyria": "GOOGLE_API_KEY",
        "google_veo": "GOOGLE_API_KEY",
        "recraft": "RECRAFT_API_KEY",
        "ideogram": "IDEOGRAM_API_KEY",
        "black_forest_labs": "BFL_API_KEY",
        "stability_ai": "STABILITY_API_KEY",
        "stability_audio": "STABILITY_API_KEY",
        "fal_ai": "FAL_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "cartesia": "CARTESIA_API_KEY",
        "playht": "PLAYHT_API_KEY",
        "deepgram": "DEEPGRAM_API_KEY",
        "runway": "RUNWAY_API_KEY",
        "kling": "KLING_API_KEY",
        "luma": "LUMA_API_KEY",
        "minimax_hailuo": "MINIMAX_API_KEY",
        "heygen": "HEYGEN_API_KEY",
        "synthesia": "SYNTHESIA_API_KEY",
        "sync_labs": "SYNC_LABS_API_KEY",
        "hedra": "HEDRA_API_KEY",
        "assemblyai": "ASSEMBLYAI_API_KEY",
        "magnific": "MAGNIFIC_API_KEY",
        "adobe_firefly": "ADOBE_FIREFLY_API_KEY",
    }
    _DEFAULT_AUTH = {
        "elevenlabs": ("xi-api-key", ""),
        "fal_ai": ("Authorization", "Key"),
        "google_image": ("x-goog-api-key", ""),
        "google_speech": ("x-goog-api-key", ""),
        "google_lyria": ("x-goog-api-key", ""),
        "google_veo": ("x-goog-api-key", ""),
        "cartesia": ("Cartesia-API-Key", ""),
        "deepgram": ("Authorization", "Token"),
        "assemblyai": ("Authorization", ""),
        "ideogram": ("Api-Key", ""),
        "black_forest_labs": ("x-key", ""),
        "anthropic_claude_script": ("x-api-key", ""),
    }
    _DEFAULT_PROBE_HEADERS = {
        "anthropic_claude_script": {"anthropic-version": "2023-06-01"},
    }

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        max_response_bytes: int = 4096,
    ) -> None:
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("environ must be a mapping of environment names to values")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes < 0
            or max_response_bytes > 1_000_000
        ):
            raise ValueError("max_response_bytes must be an integer between 0 and 1000000")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide an open method")
        self.environ = os.environ if environ is None else environ
        self.opener = opener or build_opener(_RejectRedirects())
        self.max_response_bytes = max_response_bytes

    def __call__(self, provider: dict[str, Any], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        if not isinstance(provider, dict):
            raise ValueError("provider metadata must be an object")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

        configuration = provider.get("configuration", {})
        if not isinstance(configuration, dict):
            raise ValueError("provider configuration is invalid")
        endpoint = configuration.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("ENDPOINT_NOT_CONFIGURED")
        allowed_hosts = provider.get("allowed_hosts")
        if not isinstance(allowed_hosts, (list, set, tuple)) or not allowed_hosts:
            raise ValueError("HOST_ALLOWLIST_REQUIRED")
        validation = ConnectionManager.validate_endpoint(
            endpoint,
            allowed_hosts=allowed_hosts if isinstance(allowed_hosts, (list, set, tuple)) else None,
        )
        if not validation.get("valid"):
            raise ValueError(str(validation.get("status", "INVALID_ENDPOINT")))

        headers = {
            "Accept": "application/json, */*;q=0.8",
            "User-Agent": "AI_CREATOR_AGENT-ProviderProbe/1.0",
        }
        for name, value in self._DEFAULT_PROBE_HEADERS.get(
            str(provider.get("id", "")).casefold(), {}
        ).items():
            headers[name] = value
        raw_auth_type = provider.get("auth_type", "NONE")
        if not isinstance(raw_auth_type, str) or not raw_auth_type.strip():
            raise ValueError("INVALID_AUTH_TYPE")
        auth_type = raw_auth_type.strip().upper()
        if auth_type not in {"NONE", "PUBLIC", "ANONYMOUS"}:
            if auth_type in {"ACCOUNT", "MANUAL", "MANUAL_CONNECT", "OAUTH_BROWSER"}:
                raise PermissionError("manual account connection is required")
            token_env = provider.get("token_env") or provider.get("api_key_env") or provider.get("credential_env") or self._DEFAULT_TOKEN_ENV.get(
                str(provider.get("id", "")).casefold()
            )
            if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", token_env):
                raise PermissionError("credential environment reference is not configured")
            token = self.environ.get(token_env)
            if not isinstance(token, str) or not token.strip():
                raise PermissionError("provider credential is missing")
            if len(token) > 16384 or any(ord(char) < 32 or ord(char) == 127 for char in token):
                raise PermissionError("provider credential format is invalid")
            default_header, default_scheme = self._DEFAULT_AUTH.get(
                str(provider.get("id", "")).casefold(), ("Authorization", "Bearer")
            )
            header_name = provider.get("auth_header_name", default_header)
            auth_scheme = provider.get("auth_scheme", default_scheme)
            allowed_auth_headers = {
                "authorization", "x-api-key", "api-key", "xi-api-key",
                "x-goog-api-key", "cartesia-api-key", "x-auth-token", "x-key",
            }
            if (
                not isinstance(header_name, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", header_name)
                or header_name.casefold() not in allowed_auth_headers
            ):
                raise ValueError("provider auth header name is not permitted")
            allowed_auth_schemes = {"", "bearer", "key", "token", "basic"}
            if (
                not isinstance(auth_scheme, str)
                or auth_scheme.casefold() not in allowed_auth_schemes
            ):
                raise ValueError("provider auth scheme is not permitted")
            headers[header_name] = f"{auth_scheme} {token}" if auth_scheme else token

        request = Request(endpoint.strip(), headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=float(timeout_seconds))
        except HTTPError as exc:
            status_code = exc.code
            exc.close()
            if status_code in {401, 403}:
                raise ProviderHTTPAuthError(status_code) from None
            if 300 <= status_code < 400:
                raise ConnectionError("provider endpoint redirected; redirects are not followed") from None
            retry_after = None
            try:
                raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if (
                    isinstance(raw_retry_after, str)
                    and len(raw_retry_after) <= 128
                    and not any(ord(char) < 32 or ord(char) == 127 for char in raw_retry_after)
                ):
                    retry_after = raw_retry_after
            except Exception:
                retry_after = None
            raise ProviderHTTPStatusError(status_code, retry_after) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                raise TimeoutError("provider probe timed out") from None
            raise ConnectionError("provider endpoint is unreachable") from None

        try:
            status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ConnectionError("provider did not return a successful HTTP status")
            # Bound any response read and never return or log response content.
            response.read(self.max_response_bytes + 1)
            return {"ok": True, "http_status": status}
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
