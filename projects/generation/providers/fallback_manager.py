class FallbackManager:
    @staticmethod
    def _safe_status(value):
        import re
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value):
            return value
        return "PAID_EXECUTION_FAILED"

    def __init__(self, provider_manager, provider_health=None, paid_execution_handler=None):
        self.provider_manager = provider_manager
        self.provider_health = provider_health
        # A paid handler must own approval, budget/quota reservation, execution and finalization.
        self.paid_execution_handler = paid_execution_handler

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

            if getattr(provider, "requires_paid_approval", False) is True:
                if not callable(self.paid_execution_handler):
                    errors.append({
                        "provider": provider_name,
                        "error": "PAID_EXECUTION_HANDLER_REQUIRED",
                    })
                    continue
                try:
                    paid_result = self.paid_execution_handler(
                        provider_name=provider_name,
                        provider=provider,
                        prompt=prompt,
                        output_path=output_path,
                    )
                except Exception as exc:
                    return {
                        "status": "PAID_EXECUTION_HANDLER_FAILED",
                        "provider": provider_name,
                        "error_type": type(exc).__name__,
                    }
                if not isinstance(paid_result, dict):
                    return {
                        "status": "PAID_EXECUTION_HANDLER_INVALID_RESULT",
                        "provider": provider_name,
                    }
                if paid_result.get("status") == "COMPLETED":
                    if self.provider_health:
                        self.provider_health.record_success(provider_name)
                    paid_result["attempted_providers"] = errors + [{
                        "provider": provider_name, "status": "COMPLETED",
                    }]
                    return paid_result
                # The handler owns policy-aware fallback; never silently try another paid provider.
                return {
                    "status": self._safe_status(paid_result.get("status")),
                    "provider": provider_name,
                    "error_type": paid_result.get("error_type") if isinstance(paid_result.get("error_type"), str) else None,
                }

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
                status_code = getattr(exc, "status_code", None)
                safe_status = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
                errors.append({
                    "provider": provider_name,
                    "error_type": type(exc).__name__,
                    "status_code": safe_status,
                })

        return {"status": "FAILED", "provider": None, "errors": errors}
