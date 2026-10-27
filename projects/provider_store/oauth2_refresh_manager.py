"""Provider-configured OAuth 2.0 refresh-token manager.

Network transport is injected so deployments can bind this manager to their
reviewed HTTPS transport, redirect policy, and endpoint allowlist. Tokens are
kept in process memory only; refresh-token rotation can be delivered to an
external secret manager through an injected sink.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit


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
            now = self.clock()
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
