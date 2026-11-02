"""Process-local provider health status with secret-safe failure metadata."""


class ProviderHealth:
    def __init__(self):
        self.status = {}

    def record_success(self, provider):
        self.status[provider] = {"healthy": True, "last_error": None}

    def record_failure(self, provider, error):
        status_code = getattr(error, "status_code", None)
        safe_error = (
            "Provider request failed (HTTP {})".format(status_code)
            if isinstance(status_code, int)
            and not isinstance(status_code, bool)
            and 100 <= status_code <= 599
            else "Provider request failed"
        )
        record = {
            "healthy": False,
            "last_error": safe_error,
            "last_error_type": type(error).__name__,
        }
        retryable = getattr(error, "retryable", None)
        if isinstance(retryable, bool):
            record["retryable"] = retryable
        code = getattr(error, "provider_error_code", None)
        allowed_codes = {
            "insufficient_balance",
            "key_budget_exhausted",
            "quest_pollen_only",
            "output_token_limit_exceeded",
        }
        if isinstance(code, str):
            normalized = code.strip().casefold().replace("-", "_").replace(" ", "_")
            if normalized in allowed_codes:
                record["provider_error_code"] = normalized
        self.status[provider] = record

    def is_healthy(self, provider):
        return self.status.get(provider, {}).get("healthy", True)

    def get_status(self, provider):
        return self.status.get(provider, {"healthy": True, "last_error": None})
