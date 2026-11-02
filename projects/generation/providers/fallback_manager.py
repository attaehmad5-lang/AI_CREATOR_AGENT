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
                errors.append({"provider": provider_name, "error": str(exc)})
                # Authentication, permission, billing and quota failures must
                # stop here: trying another paid provider could incur charges.
                if getattr(exc, "retryable", True) is False:
                    return {
                        "status": "FAILED",
                        "provider": None,
                        "errors": errors,
                        "fallback_stopped": True,
                        "failure_class": type(exc).__name__,
                    }

        return {"status": "FAILED", "provider": None, "errors": errors}
