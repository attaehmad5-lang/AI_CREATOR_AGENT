"""Provider fallback with billing-safe stopping and sanitized failure records."""
import re


_BILLING_CODES = {
    "insufficient_balance",
    "key_budget_exhausted",
    "quest_pollen_only",
    "payment_required",
    "output_token_limit_exceeded",
}
_HARD_STOP_STATUSES = {401, 402, 403}


def _safe_failure(provider_name, exc):
    """Build a bounded failure record without returning exception/provider text."""
    record = {
        "provider": provider_name,
        "failure_class": type(exc).__name__,
    }
    status_code = getattr(exc, "status_code", None)
    if (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and 100 <= status_code <= 599
    ):
        record["status_code"] = status_code
    code = getattr(exc, "provider_error_code", None)
    if isinstance(code, str) and len(code) <= 80:
        normalized = code.strip().casefold().replace("-", "_").replace(" ", "_")
        if re.fullmatch(r"[a-z0-9_.]+", normalized) and normalized in _BILLING_CODES:
            record["provider_error_code"] = normalized
    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        record["retryable"] = retryable
    return record


class FallbackManager:
    def __init__(self, provider_manager, provider_health=None):
        self.provider_manager = provider_manager
        self.provider_health = provider_health

    def generate_image(self, providers, prompt, output_path):
        errors = []

        for provider_name in providers:
            provider = self.provider_manager.get(provider_name)

            if provider is None:
                errors.append({"provider": provider_name, "error": "Provider not loaded"})
                continue

            if self.provider_health and not self.provider_health.is_healthy(provider_name):
                errors.append({"provider": provider_name, "error": "Provider marked unhealthy"})
                continue

            try:
                result = provider.generate_image(prompt, output_path)
                if result and result.get("status") == "COMPLETED":
                    if self.provider_health:
                        self.provider_health.record_success(provider_name)
                    result["attempted_providers"] = errors + [{"provider": provider_name, "status": "COMPLETED"}]
                    return result
                errors.append({"provider": provider_name, "error": "Generation failed"})
            except Exception as exc:
                if self.provider_health:
                    self.provider_health.record_failure(provider_name, exc)
                failure = _safe_failure(provider_name, exc)
                errors.append(failure)
                # Authentication, permission and billing failures must stop: a
                # different paid provider could create a second charge.
                hard_status = failure.get("status_code") in _HARD_STOP_STATUSES
                hard_code = failure.get("provider_error_code") in _BILLING_CODES
                if getattr(exc, "retryable", True) is False or hard_status or hard_code:
                    return {
                        "status": "FAILED",
                        "provider": None,
                        "errors": errors,
                        "fallback_stopped": True,
                        "failure_class": type(exc).__name__,
                    }

        return {"status": "FAILED", "provider": None, "errors": errors}
