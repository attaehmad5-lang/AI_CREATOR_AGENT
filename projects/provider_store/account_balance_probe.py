"""Bounded, configurable JSON account-balance probe for provider credit managers.

Only use this adapter with a provider-documented account endpoint whose selected
field means available credits (not a key budget, spending cap, or estimate).
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .connection_manager import ConnectionManager


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class ProviderJSONBalanceProbe:
    """Fetch a verified numeric balance from a configured HTTPS account endpoint."""

    _ALLOWED_HEADERS = {
        "authorization", "x-api-key", "api-key", "xi-api-key",
        "x-goog-api-key", "cartesia-api-key", "x-auth-token", "x-key",
    }
    _ALLOWED_SCHEMES = {"", "bearer", "key", "token"}

    def __init__(
        self,
        *,
        endpoint: str,
        allowed_hosts: list[str] | tuple[str, ...] | set[str],
        token_env: str,
        unit: str,
        balance_field: str = "balance",
        source: str = "provider-account-balance-api",
        auth_header_name: str = "Authorization",
        auth_scheme: str = "Bearer",
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 64 * 1024,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        authentication_required: bool = False,
        balance_field_is_available_credits: bool = False,
    ) -> None:
        if not isinstance(allowed_hosts, (list, tuple, set)) or not allowed_hosts or any(
            not isinstance(host, str) or not host.strip() for host in allowed_hosts
        ):
            raise ValueError("HOST_ALLOWLIST_REQUIRED")
        validation = ConnectionManager.validate_endpoint(endpoint, allowed_hosts=set(allowed_hosts))
        if not validation.get("valid"):
            raise ValueError(str(validation.get("status", "INVALID_ENDPOINT")))
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", token_env):
            raise ValueError("INVALID_CREDENTIAL_ENV_REFERENCE")
        if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,32}", unit):
            raise ValueError("INVALID_CREDIT_UNIT")
        if not isinstance(balance_field, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", balance_field):
            raise ValueError("INVALID_BALANCE_FIELD")
        if (
            not isinstance(source, str) or not source.strip() or len(source) > 120
            or any(marker in source.casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer "))
        ):
            raise ValueError("INVALID_CREDIT_SOURCE")
        if (
            not isinstance(auth_header_name, str)
            or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", auth_header_name)
            or auth_header_name.casefold() not in self._ALLOWED_HEADERS
        ):
            raise ValueError("INVALID_AUTH_HEADER")
        if not isinstance(auth_scheme, str) or auth_scheme.casefold() not in self._ALLOWED_SCHEMES:
            raise ValueError("INVALID_AUTH_SCHEME")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise ValueError("INVALID_TIMEOUT")
        try:
            timeout_value = float(timeout_seconds)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("INVALID_TIMEOUT") from None
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("INVALID_TIMEOUT")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or not 1 <= max_response_bytes <= 1_000_000
        ):
            raise ValueError("INVALID_RESPONSE_LIMIT")
        if authentication_required is not True:
            raise ValueError("DOCUMENTED_AUTHENTICATION_REQUIRED")
        if balance_field_is_available_credits is not True:
            raise ValueError("BALANCE_FIELD_SEMANTICS_REQUIRED")
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("INVALID_ENVIRONMENT")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("INVALID_OPENER")
        self.endpoint = endpoint.strip()
        self.allowed_hosts = tuple(allowed_hosts)
        self.token_env = token_env
        self.unit = unit
        self.balance_field = balance_field
        self.source = source.strip()
        self.auth_header_name = auth_header_name
        self.auth_scheme = auth_scheme
        self.timeout_seconds = timeout_value
        self.max_response_bytes = max_response_bytes
        self.environ = os.environ if environ is None else environ
        self.opener = opener or build_opener(_RejectRedirects())

    def __call__(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", provider_id):
            return {"status": "INVALID_PROVIDER", "verified": False}
        token = self.environ.get(self.token_env)
        if not isinstance(token, str) or not token.strip() or len(token) > 16384 or any(
            ord(char) < 32 or ord(char) == 127 for char in token
        ):
            return {"status": "CREDENTIAL_MISSING_OR_INVALID", "provider": provider_id, "verified": False}
        headers = {"Accept": "application/json", "User-Agent": "AI_CREATOR_AGENT-BalanceProbe/1.0"}
        headers[self.auth_header_name] = f"{self.auth_scheme} {token}" if self.auth_scheme else token
        request = Request(self.endpoint, headers=headers, method="GET")
        try:
            response = self.opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            status = exc.code
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
            try:
                exc.close()
            except Exception:
                pass
            result = {
                "status": "BALANCE_AUTH_FAILED" if status in {401, 403} else "BALANCE_HTTP_ERROR",
                "http_status": status if isinstance(status, int) and 100 <= status <= 599 else None,
                "provider": provider_id,
                "verified": False,
            }
            if retry_after is not None:
                result["retry_after"] = retry_after
            return result
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            status = "BALANCE_TIMEOUT" if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold() else "BALANCE_NETWORK_ERROR"
            return {"status": status, "provider": provider_id, "verified": False}
        except TimeoutError:
            return {"status": "BALANCE_TIMEOUT", "provider": provider_id, "verified": False}
        except Exception as exc:
            return {"status": "BALANCE_REQUEST_FAILED", "provider": provider_id, "verified": False, "error_type": type(exc).__name__}
        try:
            try:
                status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
                if not isinstance(status, int) or not 200 <= status < 300:
                    return {"status": "BALANCE_HTTP_ERROR", "provider": provider_id, "verified": False}
                body = response.read(self.max_response_bytes + 1)
            except TimeoutError:
                return {"status": "BALANCE_TIMEOUT", "provider": provider_id, "verified": False}
            except Exception as exc:
                return {"status": "BALANCE_RESPONSE_READ_FAILED", "provider": provider_id, "verified": False, "error_type": type(exc).__name__}
            if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
                return {"status": "BALANCE_RESPONSE_TOO_LARGE", "provider": provider_id, "verified": False}
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
                return {"status": "BALANCE_RESPONSE_INVALID", "provider": provider_id, "verified": False}
            if not isinstance(data, dict) or self.balance_field not in data:
                return {"status": "BALANCE_FIELD_MISSING", "provider": provider_id, "verified": False}
            amount = data[self.balance_field]
            if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                return {"status": "BALANCE_AMOUNT_INVALID", "provider": provider_id, "verified": False}
            try:
                numeric_amount = float(amount)
            except (OverflowError, TypeError, ValueError):
                return {"status": "BALANCE_AMOUNT_INVALID", "provider": provider_id, "verified": False}
            if not math.isfinite(numeric_amount) or numeric_amount < 0:
                return {"status": "BALANCE_AMOUNT_INVALID", "provider": provider_id, "verified": False}
            return {
                "status": "BALANCE_VERIFIED",
                "provider": provider_id,
                "verified": True,
                "free_credits_remaining": numeric_amount,
                "unit": self.unit,
                "source": self.source,
            }
        finally:
            # Response cleanup is best-effort and must not mask a verified
            # balance or a deliberately classified failure result.
            try:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
