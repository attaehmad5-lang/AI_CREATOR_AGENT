"""Provider-configured OAuth 2.0 refresh-token manager.

Network transport is injected so deployments can bind this manager to their
reviewed HTTPS transport, redirect policy, and endpoint allowlist. Tokens are
kept in process memory only; refresh-token rotation can be delivered to an
external secret manager through an injected sink.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit



class SecureOAuthTokenRequester:
    """Exchange OAuth form data through the reviewed SecureHTTPTransport."""

    def __init__(self, transport: Any, *, max_response_bytes: int = 65536) -> None:
        if not callable(getattr(transport, "request", None)):
            raise ValueError("transport must provide request")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes < 1
            or max_response_bytes > 1_048_576
        ):
            raise ValueError("max_response_bytes must be between 1 and 1048576")
        self.transport = transport
        self.max_response_bytes = max_response_bytes

    def __call__(
        self,
        endpoint: str,
        *,
        form_data: dict[str, str],
        client_auth: tuple[str, str] | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (
            parsed is None or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment
        ):
            raise RuntimeError("OAUTH_ENDPOINT_INVALID")
        transport_timeout = getattr(self.transport, "timeout", None)
        if (
            isinstance(transport_timeout, (int, float))
            and not isinstance(transport_timeout, bool)
            and transport_timeout > timeout_seconds
        ):
            raise RuntimeError("OAUTH_TRANSPORT_TIMEOUT_POLICY")
        if not isinstance(form_data, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in form_data.items()
        ):
            raise RuntimeError("OAUTH_FORM_INVALID")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if client_auth is not None:
            if (
                not isinstance(client_auth, tuple) or len(client_auth) != 2
                or not all(isinstance(value, str) and value for value in client_auth)
            ):
                raise RuntimeError("OAUTH_CLIENT_AUTH_INVALID")
            encoded = base64.b64encode(
                (client_auth[0] + ":" + client_auth[1]).encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = "Basic " + encoded
        body = urlencode(form_data).encode("utf-8")
        response = self.transport.request("POST", endpoint, headers=headers, body=body)
        if not isinstance(response, dict):
            raise RuntimeError("OAUTH_HTTP_RESPONSE_INVALID")
        status = response.get("status")
        response_body = response.get("body")
        if status != 200:
            raise RuntimeError("OAUTH_HTTP_STATUS_REJECTED")
        if not isinstance(response_body, bytes) or len(response_body) > self.max_response_bytes:
            raise RuntimeError("OAUTH_RESPONSE_SIZE_INVALID")
        try:
            parsed_body = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError("OAUTH_RESPONSE_JSON_INVALID") from None
        if not isinstance(parsed_body, dict):
            raise RuntimeError("OAUTH_RESPONSE_JSON_INVALID")
        return parsed_body


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_TOKEN = re.compile(r"[^\x00-\x20\x7f]{1,16384}\Z")


class OAuth2RefreshTokenManager:
    """Refresh and cache bearer tokens without writing credentials to disk."""

    def __init__(
        self,
        provider_configs: dict[str, dict[str, Any]],
        *,
        request_token: Callable[..., Any],
        environ: dict[str, str] | None = None,
        refresh_token_sink: Callable[[str, str], Any] | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: float = 10.0,
        expiry_skew_seconds: float = 60.0,
    ) -> None:
        if not isinstance(provider_configs, dict):
            raise ValueError("provider_configs must be a mapping")
        if not callable(request_token):
            raise ValueError("request_token must be callable")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if refresh_token_sink is not None and not callable(refresh_token_sink):
            raise ValueError("refresh_token_sink must be callable")
        if (
            not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 120
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 120")
        if (
            not isinstance(expiry_skew_seconds, (int, float)) or isinstance(expiry_skew_seconds, bool)
            or not math.isfinite(expiry_skew_seconds) or expiry_skew_seconds < 0 or expiry_skew_seconds > 600
        ):
            raise ValueError("expiry_skew_seconds must be finite and between 0 and 600")
        self.configs = {
            provider_id: self._validate_config(provider_id, config)
            for provider_id, config in provider_configs.items()
        }
        self.request_token = request_token
        self.environ = environ if environ is not None else os.environ
        self.refresh_token_sink = refresh_token_sink
        self.clock = clock or time.time
        self.timeout_seconds = float(timeout_seconds)
        self.expiry_skew_seconds = float(expiry_skew_seconds)
        self._access_tokens: dict[str, tuple[str, float]] = {}
        self._rotated_refresh_tokens: dict[str, str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _validate_config(provider_id: str, config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", provider_id):
            raise ValueError("provider ID is invalid")
        if not isinstance(config, dict):
            raise ValueError("provider OAuth config must be an object")
        endpoint = config.get("token_endpoint")
        try:
            parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
            port = parsed.port if parsed is not None else None
        except ValueError:
            parsed, port = None, None
        allowed_hosts = config.get("allowed_hosts")
        if (
            parsed is None or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or port not in (None, 443)
            or not isinstance(allowed_hosts, list) or not allowed_hosts
            or any(not isinstance(host, str) or not host.strip() for host in allowed_hosts)
            or parsed.hostname.casefold().rstrip(".") not in {
                host.casefold().rstrip(".") for host in allowed_hosts
            }
        ):
            raise ValueError("OAuth token endpoint must be HTTPS and match an explicit allowed host")
        env_fields = ("client_id_env", "client_secret_env", "refresh_token_env")
        for field in env_fields:
            if not isinstance(config.get(field), str) or not _ENV_NAME.fullmatch(config[field]):
                raise ValueError(f"{field} must name an environment variable")
        auth_method = config.get("client_auth_method", "body")
        if auth_method not in {"body", "basic"}:
            raise ValueError("client_auth_method must be body or basic")
        scope = config.get("scope", "")
        if (
            not isinstance(scope, str) or len(scope) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in scope)
        ):
            raise ValueError("scope is invalid")
        rotation_required = config.get("refresh_token_rotation_required", False)
        if not isinstance(rotation_required, bool):
            raise ValueError("refresh_token_rotation_required must be boolean")
        return {
            "token_endpoint": endpoint,
            "client_id_env": config["client_id_env"],
            "client_secret_env": config["client_secret_env"],
            "refresh_token_env": config["refresh_token_env"],
            "client_auth_method": auth_method,
            "scope": scope,
            "refresh_token_rotation_required": rotation_required,
        }

    @staticmethod
    def _secret(value: Any) -> bool:
        return isinstance(value, str) and bool(_TOKEN.fullmatch(value))

    def get_access_token(self, provider_id: str) -> str:
        """Return a valid cached token or refresh it using the configured transport."""
        with self._lock:
            config = self.configs.get(provider_id)
            if config is None:
                raise RuntimeError("OAUTH_PROVIDER_NOT_CONFIGURED")
            cached = self._access_tokens.get(provider_id)
            try:
                now = self.clock()
            except Exception:
                raise RuntimeError("OAUTH_CLOCK_FAILED") from None
            if (
                not isinstance(now, (int, float)) or isinstance(now, bool)
                or not math.isfinite(now)
            ):
                raise RuntimeError("OAUTH_CLOCK_INVALID")
            if cached is not None and cached[1] > now + self.expiry_skew_seconds:
                return cached[0]

            client_id = self.environ.get(config["client_id_env"])
            client_secret = self.environ.get(config["client_secret_env"])
            refresh_token = self._rotated_refresh_tokens.get(provider_id) or self.environ.get(
                config["refresh_token_env"]
            )
            if not all(self._secret(value) for value in (client_id, client_secret, refresh_token)):
                raise RuntimeError("OAUTH_CREDENTIALS_UNAVAILABLE")

            form = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            client_auth = None
            if config["client_auth_method"] == "body":
                form["client_id"] = client_id
                form["client_secret"] = client_secret
            else:
                client_auth = (client_id, client_secret)
            if config["scope"]:
                form["scope"] = config["scope"]

            try:
                response = self.request_token(
                    config["token_endpoint"],
                    form_data=form,
                    client_auth=client_auth,
                    timeout_seconds=self.timeout_seconds,
                )
            except Exception as exc:
                raise RuntimeError("OAUTH_TOKEN_REQUEST_FAILED") from None
            if not isinstance(response, dict):
                raise RuntimeError("OAUTH_TOKEN_RESPONSE_INVALID")
            access_token = response.get("access_token")
            token_type = response.get("token_type", "Bearer")
            expires_in = response.get("expires_in")
            if (
                not self._secret(access_token)
                or not isinstance(token_type, str)
                or token_type.casefold() != "bearer"
                or not isinstance(expires_in, (int, float))
                or isinstance(expires_in, bool)
                or not math.isfinite(expires_in)
                or expires_in <= 0
                or expires_in > 31_536_000
            ):
                raise RuntimeError("OAUTH_TOKEN_RESPONSE_INVALID")

            rotated = response.get("refresh_token")
            if rotated is None and config["refresh_token_rotation_required"]:
                raise RuntimeError("OAUTH_REFRESH_TOKEN_ROTATION_REQUIRED")
            if rotated is not None:
                if not self._secret(rotated):
                    raise RuntimeError("OAUTH_REFRESH_TOKEN_INVALID")
                if rotated != refresh_token:
                    if self.refresh_token_sink is not None:
                        try:
                            sink_result = self.refresh_token_sink(provider_id, rotated)
                        except Exception:
                            if config["refresh_token_rotation_required"]:
                                raise RuntimeError("OAUTH_REFRESH_TOKEN_PERSISTENCE_FAILED") from None
                        else:
                            if sink_result is not True and config["refresh_token_rotation_required"]:
                                raise RuntimeError("OAUTH_REFRESH_TOKEN_PERSISTENCE_FAILED")
                    elif config["refresh_token_rotation_required"]:
                        raise RuntimeError("OAUTH_REFRESH_TOKEN_SINK_REQUIRED")
                    self._rotated_refresh_tokens[provider_id] = rotated

            self._access_tokens[provider_id] = (access_token, now + float(expires_in))
            return access_token

    def invalidate(self, provider_id: str) -> bool:
        """Discard only the in-memory access-token cache for a configured provider."""
        with self._lock:
            if provider_id not in self.configs:
                return False
            self._access_tokens.pop(provider_id, None)
            return True
