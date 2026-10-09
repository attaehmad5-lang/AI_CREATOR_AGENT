class ProviderHealth:
    def __init__(self):
        self.status = {}

    def record_success(self, provider):
        self.status[provider] = {"healthy": True, "last_error": None}

    def record_failure(self, provider, error):
        self.status[provider] = {"healthy": False, "last_error": str(error)}

    def is_healthy(self, provider):
        return self.status.get(provider, {}).get("healthy", True)

    def get_status(self, provider):
        return self.status.get(provider, {"healthy": True, "last_error": None})
