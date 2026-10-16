import re

class FallbackManager:
    @staticmethod
    def _safe_status(value):
        import re
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value):
            return value
        return "PAID_EXECUTION_FAILED"

    def __init__(
        self, provider_manager, provider_health=None, paid_execution_handler=None,
        execution_limiter=None, provider_ids=None,
    ):
        self.provider_manager = provider_manager
        self.provider_health = provider_health
        # A paid handler must own approval, budget/quota reservation, execution and finalization.
        self.paid_execution_handler = paid_execution_handler
        if execution_limiter is not None:
            try:
                valid_limiter = callable(getattr(execution_limiter, "acquire", None)) and callable(
                    getattr(execution_limiter, "release", None)
                )
            except Exception:
                valid_limiter = False
            if not valid_limiter:
                raise ValueError("execution_limiter must provide acquire and release methods")
        if provider_ids is not None and not isinstance(provider_ids, dict):
            raise ValueError("provider_ids must map display names to canonical provider IDs")
        self.execution_limiter = execution_limiter
        self.provider_ids = dict(provider_ids or {})

    def _run_with_optional_slot(
        self, provider_name, provider, operation, *, request_id, queue_timeout_seconds
    ):
        if self.execution_limiter is None:
            try:
                return {"kind": "result", "result": operation()}
            except Exception as exc:
                return {"kind": "exception", "exception": exc}

        provider_id = self.provider_ids.get(provider_name)
        if not isinstance(provider_id, str) or not provider_id.strip():
            try:
                provider_id = getattr(provider, "provider_id", None) or getattr(provider, "id", None)
            except Exception:
                provider_id = None
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"kind": "rejected", "status": "EXECUTION_LIMITER_PROVIDER_ID_REQUIRED"}
        if not isinstance(request_id, str) or not request_id.strip():
            return {"kind": "rejected", "status": "REQUEST_ID_REQUIRED"}
        try:
            admission = self.execution_limiter.acquire(
                provider_id, request_id, timeout_seconds=queue_timeout_seconds
            )
        except Exception as exc:
            return {
                "kind": "rejected",
                "status": "EXECUTION_ADMISSION_FAILED",
                "error_type": type(exc).__name__,
            }
        if not isinstance(admission, dict) or admission.get("acquired") is not True:
            status = admission.get("status") if isinstance(admission, dict) else None
            if not isinstance(status, str) or not re.fullmatch(r"[A-Z0-9_]{1,80}", status):
                status = "EXECUTION_ADMISSION_REJECTED"
            return {"kind": "rejected", "status": status}

        result = None
        operation_error = None
        try:
            result = operation()
        except Exception as exc:
            operation_error = exc
        try:
            released = self.execution_limiter.release(provider_id, request_id)
        except Exception as exc:
            return {
                "kind": "rejected",
                "status": "EXECUTION_SLOT_RELEASE_FAILED",
                "error_type": type(exc).__name__,
            }
        if not isinstance(released, dict) or released.get("released") is not True:
            return {"kind": "rejected", "status": "EXECUTION_SLOT_RELEASE_FAILED"}
        if operation_error is not None:
            return {"kind": "exception", "exception": operation_error}
        return {"kind": "result", "result": result}

    def generate_image(
        self, providers, prompt, output_path, *, request_id=None, queue_timeout_seconds=0.0
    ):
        if self.execution_limiter is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            return {"status": "REQUEST_ID_REQUIRED"}
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
                    # Do not silently route sensitive prompts to another provider when
                    # the selected provider requires paid-use approval/policy handling.
                    return {
                        "status": "PAID_EXECUTION_HANDLER_REQUIRED",
                        "provider": provider_name,
                        "attempted_providers": errors + [{
                            "provider": provider_name,
                            "status": "PAID_EXECUTION_HANDLER_REQUIRED",
                        }],
                    }
                invocation = self._run_with_optional_slot(
                    provider_name, provider,
                    lambda: self.paid_execution_handler(
                        provider_name=provider_name,
                        provider=provider,
                        prompt=prompt,
                        output_path=output_path,
                        request_id=request_id,
                    ),
                    request_id=request_id,
                    queue_timeout_seconds=queue_timeout_seconds,
                )
                if invocation["kind"] == "rejected":
                    return {
                        "status": invocation["status"],
                        "provider": provider_name,
                        "error_type": invocation.get("error_type"),
                    }
                if invocation["kind"] == "exception":
                    exc = invocation["exception"]
                    return {
                        "status": "PAID_EXECUTION_HANDLER_FAILED",
                        "provider": provider_name,
                        "error_type": type(exc).__name__,
                    }
                paid_result = invocation["result"]
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

            invocation = self._run_with_optional_slot(
                provider_name, provider,
                lambda: provider.generate_image(prompt, output_path),
                request_id=request_id,
                queue_timeout_seconds=queue_timeout_seconds,
            )
            if invocation["kind"] == "rejected":
                # Admission failure is not a provider failure; do not forward the prompt elsewhere.
                return {
                    "status": invocation["status"],
                    "provider": provider_name,
                    "error_type": invocation.get("error_type"),
                }
            if invocation["kind"] == "exception":
                exc = invocation["exception"]
                if self.provider_health:
                    self.provider_health.record_failure(provider_name, exc)
                status_code = getattr(exc, "status_code", None)
                safe_status = status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None
                errors.append({
                    "provider": provider_name,
                    "error_type": type(exc).__name__,
                    "status_code": safe_status,
                })
                continue

            result = invocation["result"]
            if isinstance(result, dict) and result.get("status") == "COMPLETED":
                if self.provider_health:
                    self.provider_health.record_success(provider_name)
                result["attempted_providers"] = errors + [{"provider": provider_name, "status": "COMPLETED"}]
                return result
            errors.append({"provider": provider_name, "error": "Generation failed"})

        return {"status": "FAILED", "provider": None, "errors": errors}
