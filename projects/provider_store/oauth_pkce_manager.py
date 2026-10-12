"""User-authorized OAuth 2.0 authorization-code + PKCE helper.

Tokens and pending PKCE verifiers remain in process memory only. This module never
persists tokens, never logs response bodies, never follows redirects, and never
starts paid activity. Providers without refresh tokens must restart their consent flow.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connection_manager import ConnectionManager


class _RejectOAuthRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class ProviderOAuthPKCEManager:
    """In-memory OAuth PKCE flow and access-token lifecycle."""

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        clock: Callable[[], float] | None = None,
        state_ttl_seconds: int = 600,
        max_response_bytes: int = 64 * 1024,
    ) -> None:
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("environ must be a mapping")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide an open method")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if not isinstance(state_ttl_seconds, int) or isinstance(state_ttl_seconds, bool) or not 60 <= state_ttl_seconds <= 1800:
            raise ValueError("state_ttl_seconds must be between 60 and 1800")
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or not 256 <= max_response_bytes <= 1_000_000:
            raise ValueError("max_response_bytes must be between 256 and 1000000")
        self.environ = environ
        self.opener = opener or build_opener(_RejectOAuthRedirects())
        self.clock = clock or time.time
        self.state_ttl_seconds = state_ttl_seconds
        self.max_response_bytes = max_response_bytes
        self._lock = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._refreshing: set[str] = set()
        # Invalidate token exchanges already in flight when a user clears a provider.
        self._generation: dict[str, int] = {}

    @staticmethod
    def _safe_provider_id(provider_id: Any) -> bool:
        return isinstance(provider_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", provider_id))

    @staticmethod
    def _valid_redirect_uri(redirect_uri: Any) -> bool:
        if not isinstance(redirect_uri, str) or not redirect_uri.strip() or any(ord(c) < 32 or ord(c) == 127 for c in redirect_uri):
            return False
        try:
            parsed = urlsplit(redirect_uri)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return False
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and host.casefold().rstrip(".") in {"localhost", "127.0.0.1", "::1"} and (port is None or 1 <= port <= 65535)

    @staticmethod
    def _valid_endpoint(endpoint: Any, allowed_hosts: Any) -> bool:
        if not isinstance(allowed_hosts, (list, tuple, set, frozenset)) or not allowed_hosts:
            return False
        return ConnectionManager.validate_endpoint(
            endpoint,
            allowed_hosts=allowed_hosts,
        ).get("valid") is True

    @staticmethod
    def _valid_token(value: Any) -> bool:
        return (
            isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 16384
            and all(0x21 <= ord(char) <= 0x7E for char in value)
        )

    def begin_authorization(
        self,
        provider_id: str,
        *,
        authorization_url: str,
        token_url: str,
        client_id_env: str,
        redirect_uri: str,
        allowed_redirect_uris: list[str] | tuple[str, ...] | set[str],
        allowed_hosts: list[str] | tuple[str, ...] | set[str],
        scopes: str | list[str] | tuple[str, ...] = "",
    ) -> dict[str, Any]:
        if not self._safe_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER", "provider": provider_id}
        if not self._valid_endpoint(authorization_url, allowed_hosts) or not self._valid_endpoint(token_url, allowed_hosts):
            return {"status": "UNSAFE_OAUTH_ENDPOINT", "provider": provider_id}
        if (
            not isinstance(allowed_redirect_uris, (list, tuple, set, frozenset))
            or not allowed_redirect_uris
            or any(not self._valid_redirect_uri(item) for item in allowed_redirect_uris)
            or not self._valid_redirect_uri(redirect_uri)
            or redirect_uri not in allowed_redirect_uris
        ):
            return {"status": "INVALID_REDIRECT_URI", "provider": provider_id}
        if not isinstance(client_id_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", client_id_env):
            return {"status": "INVALID_CLIENT_ID_REFERENCE", "provider": provider_id}
        environment = self.environ
        if environment is None:
            import os
            environment = os.environ
        client_id = environment.get(client_id_env)
        if not self._valid_token(client_id):
            return {"status": "CLIENT_ID_MISSING_OR_INVALID", "provider": provider_id}
        if isinstance(scopes, (list, tuple)):
            if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9:._/-]{1,100}", item) for item in scopes):
                return {"status": "INVALID_SCOPES", "provider": provider_id}
            scope_text = " ".join(scopes)
        elif isinstance(scopes, str):
            scope_text = scopes.strip()
        else:
            return {"status": "INVALID_SCOPES", "provider": provider_id}
        if scope_text and not re.fullmatch(r"[A-Za-z0-9:._/-]+(?: [A-Za-z0-9:._/-]+)*", scope_text):
            return {"status": "INVALID_SCOPES", "provider": provider_id}
        try:
            now = float(self.clock())
        except Exception:
            return {"status": "CLOCK_INVALID", "provider": provider_id}
        if not math.isfinite(now):
            return {"status": "CLOCK_INVALID", "provider": provider_id}

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        parsed = urlsplit(authorization_url)
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if scope_text:
            query["scope"] = scope_text
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        with self._lock:
            self._pending[state] = {
                "provider_id": provider_id,
                "generation": self._generation.get(provider_id, 0),
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "token_url": token_url,
                "allowed_hosts": list(allowed_hosts),
                "verifier": verifier,
                "scope": scope_text,
                "expires_at": now + self.state_ttl_seconds,
            }
            # Expired states are discarded opportunistically; no background task is needed.
            self._pending = {
                key: value for key, value in self._pending.items()
                if value.get("expires_at", 0) > now
            }
            while len(self._pending) > 1000:
                self._pending.pop(next(iter(self._pending)))
        return {
            "status": "AUTHORIZATION_REQUIRED",
            "provider": provider_id,
            "authorization_url": url,
            "state": state,
            "expires_at": self._pending[state]["expires_at"],
            "pkce_method": "S256",
        }

    def _post_form(self, url: str, allowed_hosts: list[str], form: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
        if not self._valid_endpoint(url, allowed_hosts):
            return {"status": "UNSAFE_OAUTH_ENDPOINT"}
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return {"status": "INVALID_TIMEOUT"}
        request = Request(
            url,
            data=urlencode(form).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            response = self.opener.open(request, timeout=float(timeout_seconds))
        except HTTPError as exc:
            status_code = exc.code
            exc.close()
            return {"status": "OAUTH_TOKEN_HTTP_ERROR", "http_status": status_code}
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                return {"status": "OAUTH_TOKEN_TIMEOUT"}
            return {"status": "OAUTH_TOKEN_NETWORK_ERROR"}
        except TimeoutError:
            return {"status": "OAUTH_TOKEN_TIMEOUT"}
        except Exception as exc:
            return {"status": "OAUTH_TOKEN_REQUEST_FAILED", "error_type": type(exc).__name__}
        try:
            try:
                status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
                if not isinstance(status, int) or not 200 <= status < 300:
                    return {"status": "OAUTH_TOKEN_HTTP_ERROR", "http_status": status if isinstance(status, int) else None}
                body = response.read(self.max_response_bytes + 1)
            except TimeoutError:
                return {"status": "OAUTH_TOKEN_TIMEOUT"}
            except OSError:
                return {"status": "OAUTH_TOKEN_NETWORK_ERROR"}
            except Exception as exc:
                return {"status": "OAUTH_TOKEN_RESPONSE_READ_FAILED", "error_type": type(exc).__name__}
            if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
                return {"status": "OAUTH_TOKEN_RESPONSE_TOO_LARGE"}
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return {"status": "OAUTH_TOKEN_RESPONSE_INVALID"}
            if not isinstance(data, dict):
                return {"status": "OAUTH_TOKEN_RESPONSE_INVALID"}
            return {"status": "OK", "data": data}
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def _install_tokens(self, provider_id: str, pending: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        access_token = data.get("access_token")
        token_type = data.get("token_type", "Bearer")
        expires_in = data.get("expires_in")
        if not self._valid_token(access_token):
            return {"status": "OAUTH_ACCESS_TOKEN_INVALID", "provider": provider_id}
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            return {"status": "OAUTH_TOKEN_TYPE_UNSUPPORTED", "provider": provider_id}
        if not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or not math.isfinite(expires_in) or not 1 <= expires_in <= 31_536_000:
            return {"status": "OAUTH_EXPIRY_INVALID", "provider": provider_id}
        try:
            now = float(self.clock())
        except Exception:
            return {"status": "CLOCK_INVALID", "provider": provider_id}
        if not math.isfinite(now):
            return {"status": "CLOCK_INVALID", "provider": provider_id}
        refresh_token = data.get("refresh_token")
        if refresh_token is not None and not self._valid_token(refresh_token):
            return {"status": "OAUTH_REFRESH_TOKEN_INVALID", "provider": provider_id}
        raw_scope = data.get("scope", pending.get("scope", ""))
        if not isinstance(raw_scope, str) or len(raw_scope) > 2000 or any(ord(char) < 32 for char in raw_scope):
            return {"status": "OAUTH_SCOPE_INVALID", "provider": provider_id}
        with self._lock:
            expected_generation = pending.get("generation", self._generation.get(provider_id, 0))
            if expected_generation != self._generation.get(provider_id, 0):
                return {"status": "OAUTH_SESSION_CLEARED", "provider": provider_id}
            self._tokens[provider_id] = {
                "access_token": access_token,
                "generation": expected_generation,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_at": now + float(expires_in),
                "token_url": pending["token_url"],
                "allowed_hosts": list(pending["allowed_hosts"]),
                "client_id": pending["client_id"],
                "scope": raw_scope,
            }
        return {
            "status": "OAUTH_CONNECTED",
            "provider": provider_id,
            "token_type": "Bearer",
            "expires_at": now + float(expires_in),
            "scopes": raw_scope.split() if raw_scope else [],
            "refresh_available": refresh_token is not None,
        }

    def exchange_code(self, provider_id: str, *, state: str, code: str, timeout_seconds: float = 10.0) -> dict[str, Any]:
        if not self._safe_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER", "provider": provider_id}
        if not isinstance(state, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,200}", state):
            return {"status": "INVALID_OAUTH_STATE", "provider": provider_id}
        if not isinstance(code, str) or not code or code != code.strip() or len(code) > 4096 or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in code):
            return {"status": "INVALID_AUTHORIZATION_CODE", "provider": provider_id}
        with self._lock:
            pending = self._pending.get(state)
            if not isinstance(pending, dict) or pending.get("provider_id") != provider_id:
                return {"status": "OAUTH_STATE_NOT_FOUND", "provider": provider_id}
            self._pending.pop(state, None)
        try:
            now = float(self.clock())
        except Exception:
            return {"status": "CLOCK_INVALID", "provider": provider_id}
        if not math.isfinite(now) or now >= pending["expires_at"]:
            return {"status": "OAUTH_STATE_EXPIRED", "provider": provider_id}
        result = self._post_form(
            pending["token_url"], pending["allowed_hosts"],
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "code_verifier": pending["verifier"],
            },
            timeout_seconds,
        )
        if result.get("status") != "OK":
            failure = {
                "status": result.get("status", "OAUTH_TOKEN_REQUEST_FAILED"),
                "provider": provider_id,
            }
            http_status = result.get("http_status")
            if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
                failure["http_status"] = http_status
            return failure
        return self._install_tokens(provider_id, pending, result["data"])

    def refresh(self, provider_id: str, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        with self._lock:
            record = self._tokens.get(provider_id) if isinstance(provider_id, str) else None
            if not isinstance(record, dict):
                return {"status": "OAUTH_SESSION_NOT_FOUND", "provider": provider_id}
            refresh_token = record.get("refresh_token")
            if not self._valid_token(refresh_token):
                return {"status": "REFRESH_NOT_SUPPORTED", "provider": provider_id}
            if provider_id in self._refreshing:
                return {"status": "REFRESH_IN_PROGRESS", "provider": provider_id}
            self._refreshing.add(provider_id)
            record = dict(record)
        try:
            result = self._post_form(
                record["token_url"], record["allowed_hosts"],
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": record["client_id"],
                },
                timeout_seconds,
            )
            if result.get("status") != "OK":
                failure = {
                    "status": result.get("status", "OAUTH_TOKEN_REQUEST_FAILED"),
                    "provider": provider_id,
                }
                http_status = result.get("http_status")
                if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
                    failure["http_status"] = http_status
                return failure
            pending = {
                "token_url": record["token_url"],
                "allowed_hosts": record["allowed_hosts"],
                "client_id": record["client_id"],
                "scope": record.get("scope", ""),
                "generation": record.get("generation", self._generation.get(provider_id, 0)),
            }
            data = dict(result["data"])
            data.setdefault("refresh_token", refresh_token)
            return self._install_tokens(provider_id, pending, data)
        finally:
            with self._lock:
                self._refreshing.discard(provider_id)

    def get_access_token(self, provider_id: str) -> str | None:
        with self._lock:
            record = self._tokens.get(provider_id) if isinstance(provider_id, str) else None
            if not isinstance(record, dict):
                return None
            try:
                now = float(self.clock())
            except Exception:
                return None
            if not math.isfinite(now) or now >= record.get("expires_at", 0):
                return None
            token = record.get("access_token")
            return token if self._valid_token(token) else None

    def status(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._tokens.get(provider_id) if isinstance(provider_id, str) else None
            if not isinstance(record, dict):
                return {"status": "DISCONNECTED", "provider": provider_id, "connected": False}
            try:
                now = float(self.clock())
            except Exception:
                return {"status": "CLOCK_INVALID", "provider": provider_id, "connected": False}
            if not math.isfinite(now):
                return {"status": "CLOCK_INVALID", "provider": provider_id, "connected": False}
            if now >= record.get("expires_at", 0):
                return {
                    "status": "TOKEN_EXPIRED", "provider": provider_id,
                    "connected": False, "refresh_available": self._valid_token(record.get("refresh_token")),
                }
            return {
                "status": "CONNECTED", "provider": provider_id,
                "connected": True, "expires_at": record["expires_at"],
                "scopes": record.get("scope", "").split(),
                "refresh_available": self._valid_token(record.get("refresh_token")),
            }

    def clear(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            if provider_id in self._refreshing:
                return {"status": "REFRESH_IN_PROGRESS", "provider": provider_id}
            self._pending = {
                state: record for state, record in self._pending.items()
                if record.get("provider_id") != provider_id
            }
            existed = self._tokens.pop(provider_id, None) is not None
            self._generation[provider_id] = self._generation.get(provider_id, 0) + 1
            return {"status": "CLEARED" if existed else "NOT_CONNECTED", "provider": provider_id}
