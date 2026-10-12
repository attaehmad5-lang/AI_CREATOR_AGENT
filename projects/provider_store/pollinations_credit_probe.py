"""Read-only Pollinations account-balance probe.

The endpoint reports a remaining pollen budget that may combine tier, pack, and
crypto balances. It does NOT identify which portion is free, so this result must
never be passed to ProviderCreditManager or treated as free-credit authorization.
A user-authorized, unexpired access token is supplied in memory only.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .http_probe import ProviderHTTPStatusError


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class PollinationsBalanceProbe:
    """Adapt Pollinations' GET /account/balance response to ProviderCreditManager."""

    ENDPOINT = "https://gen.pollinations.ai/account/balance"
    ALLOWED_HOST = "gen.pollinations.ai"

    def __init__(
        self,
        token_provider: Callable[[], str | None],
        *,
        opener: Any = None,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 16_384,
    ) -> None:
        if not callable(token_provider):
            raise ValueError("token_provider must be callable")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 60
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 60")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes < 2
            or max_response_bytes > 1_000_000
        ):
            raise ValueError("max_response_bytes must be between 2 and 1000000")
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise ValueError("opener must provide an open method")
        self.token_provider = token_provider
        self.opener = opener or build_opener(_RejectRedirects())
        self.timeout_seconds = float(timeout_seconds)
        self.max_response_bytes = max_response_bytes

    def __call__(self, provider_id: str) -> dict[str, Any]:
        if provider_id != "pollinations":
            raise ValueError("unsupported credit provider")
        token = self.token_provider()
        if not isinstance(token, str) or not token.strip():
            raise PermissionError("user-authorized Pollinations token is unavailable")
        if len(token) > 16_384 or any(ord(char) < 32 or ord(char) == 127 for char in token):
            raise PermissionError("user-authorized Pollinations token is invalid")

        request = Request(
            self.ENDPOINT,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "AI_CREATOR_AGENT-CreditProbe/1.0",
            },
            method="GET",
        )
        try:
            response = self.opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status in {401, 403}:
                raise PermissionError("Pollinations balance access was rejected; verify token and account scope") from None
            if 300 <= status < 400:
                raise ConnectionError("Pollinations balance endpoint redirected; redirects are not followed") from None
            retry_after = None
            raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if (
                isinstance(raw_retry_after, str)
                and len(raw_retry_after) <= 128
                and not any(ord(char) < 32 or ord(char) == 127 for char in raw_retry_after)
            ):
                retry_after = raw_retry_after
            raise ProviderHTTPStatusError(status, retry_after) from None
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or "timeout" in type(reason).__name__.casefold():
                raise TimeoutError("Pollinations balance probe timed out") from None
            raise ConnectionError("Pollinations balance endpoint is unreachable") from None

        try:
            status = response.getcode() if callable(getattr(response, "getcode", None)) else getattr(response, "status", None)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise ConnectionError("Pollinations balance endpoint did not return a successful HTTP status")
            body = response.read(self.max_response_bytes + 1)
            if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
                raise ValueError("Pollinations balance response exceeds the configured size limit")
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                raise ValueError("Pollinations balance response is not valid JSON") from None
            if not isinstance(data, dict):
                raise ValueError("Pollinations balance response must be an object")
            balance = data.get("balance")
            if (
                not isinstance(balance, (int, float))
                or isinstance(balance, bool)
                or not math.isfinite(balance)
                or balance < 0
            ):
                raise ValueError("Pollinations balance response contains an invalid balance")
            return {
                "verified": True,
                "balance": float(balance),
                "unit": "pollen",
                "balance_type": "remaining_account_budget",
                "source": "pollinations.account.balance",
            }
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
