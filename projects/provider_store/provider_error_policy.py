"""Normalize provider billing/quota failures without exposing provider response bodies.

The classifier intentionally accepts only HTTP status and an explicit, bounded provider
error code. It never guesses credit balance from a message, response body, or catalog label.
"""
from __future__ import annotations

import math
import re
from typing import Any


_CREDIT_EXHAUSTED_CODES = {
    "insufficient_credits",
    "insufficient_credit",
    "insufficient_quota",
    "credits_exhausted",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "quota_exhausted",
}
_BILLING_ACTION_CODES = {
    "payment_required",
    "billing_required",
    "account_payment_required",
}
_AUTH_CODES = {"invalid_api_key", "invalid_token", "unauthorized"}
_PERMISSION_CODES = {"permission_denied", "insufficient_scope", "forbidden"}
_TRANSIENT_CODES = {"temporarily_unavailable", "overloaded", "server_overloaded"}


def _normalize_code(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 80:
        return None
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if not normalized or not re.fullmatch(r"[a-z0-9_.]+", normalized):
        return None
    return normalized


def _retry_after(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        seconds = float(value)
    except (OverflowError, ValueError):
        # Huge integers can overflow during float conversion before isfinite()
        # gets a chance to reject them. Treat malformed retry metadata as absent.
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, 300.0)


def classify_provider_error(
    http_status: Any,
    *,
    provider_code: Any = None,
    retry_after_seconds: Any = None,
) -> dict[str, Any]:
    """Return a safe, normalized action for a provider failure.

    Retry is never recommended for billing, exhausted-credit, authentication, permission,
    or malformed-request errors. For 429, a numeric bounded Retry-After is surfaced only
    when the error is not explicitly classified as exhausted credits.
    """
    if (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        return {
            "status": "INVALID_PROVIDER_ERROR_INPUT",
            "category": "invalid_input",
            "retryable": False,
            "action": "inspect_local_call_site",
        }

    code = _normalize_code(provider_code)
    if http_status == 402 or code in _BILLING_ACTION_CODES:
        return {
            "status": "BILLING_ACTION_REQUIRED",
            "category": "billing",
            "retryable": False,
            "action": "stop_and_request_account_or_payment_review",
            "http_status": http_status,
        }
    if code in _CREDIT_EXHAUSTED_CODES:
        return {
            "status": "CREDITS_EXHAUSTED",
            "category": "credits_exhausted",
            "retryable": False,
            "action": "stop_paid_attempt_and_refresh_verified_balance",
            "http_status": http_status,
        }
    if http_status == 401 or code in _AUTH_CODES:
        return {
            "status": "AUTHENTICATION_FAILED",
            "category": "authentication",
            "retryable": False,
            "action": "verify_credentials_without_logging_them",
            "http_status": http_status,
        }
    if http_status == 403 or code in _PERMISSION_CODES:
        return {
            "status": "PERMISSION_DENIED",
            "category": "permission",
            "retryable": False,
            "action": "review_provider_permissions_and_scopes",
            "http_status": http_status,
        }
    if http_status == 429:
        result = {
            "status": "RATE_LIMITED",
            "category": "rate_limit",
            "retryable": True,
            "action": "wait_before_retrying_within_policy",
            "http_status": http_status,
        }
        delay = _retry_after(retry_after_seconds)
        if delay is not None:
            result["retry_after_seconds"] = delay
        return result
    if http_status in {408, 425} or http_status >= 500 or code in _TRANSIENT_CODES:
        return {
            "status": "PROVIDER_TEMPORARILY_UNAVAILABLE",
            "category": "transient",
            "retryable": True,
            "action": "retry_with_bounded_backoff_and_idempotency",
            "http_status": http_status,
        }
    if 400 <= http_status <= 499:
        return {
            "status": "PROVIDER_REQUEST_REJECTED",
            "category": "request",
            "retryable": False,
            "action": "correct_request_or_provider_configuration",
            "http_status": http_status,
        }
    return {
        "status": "PROVIDER_ERROR_UNCLASSIFIED",
        "category": "unknown",
        "retryable": False,
        "action": "fail_closed_and_review_safe_diagnostics",
        "http_status": http_status,
    }
