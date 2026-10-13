class ProviderHealth:
    def __init__(self):
        self.status = {}

    def record_success(self, provider):
        self.status[provider] = {"healthy": True, "last_error": None}

    def record_failure(self, provider, error):
        # Exceptions can include request URLs, headers, or credential fragments.
        # Persist only safe classifications, never arbitrary exception text.
        status_code = getattr(error, "status_code", None)
        safe_status = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
        self.status[provider] = {
            "healthy": False,
            "last_error": type(error).__name__,
            "last_error_status": safe_status,
        }

    def is_healthy(self, provider):
        return self.status.get(provider, {}).get("healthy", True)

    def get_status(self, provider):
        return self.status.get(provider, {"healthy": True, "last_error": None})
