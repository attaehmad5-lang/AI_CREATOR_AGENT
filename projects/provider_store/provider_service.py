"""Integration facade for provider connection, routing, health, cost, quota, and outputs."""
from __future__ import annotations

from typing import Any
import re
import time

from .safe_numbers import finite_number
from .provider_error_policy import classify_provider_error


class ProviderService:
    """Coordinate existing provider managers without inventing live connection evidence."""

    def __init__(
        self,
        connection_manager: Any,
        *,
        routing_manager: Any | None = None,
        health_manager: Any | None = None,
        retry_policy: Any | None = None,
        output_validator: Any | None = None,
        cost_policy: Any | None = None,
        quota_manager: Any | None = None,
        credit_manager: Any | None = None,
        budget_alert_manager: Any | None = None,
        authorization_manager: Any | None = None,
        degraded_mode_manager: Any | None = None,
        sensitive_data_policy: Any | None = None,
        data_handling_policy: Any | None = None,
        webhook_verifier: Any | None = None,
        job_manager: Any | None = None,
        metrics_manager: Any | None = None,
        quality_policy: Any | None = None,
        benchmark_manager: Any | None = None,
        incident_manager: Any | None = None,
        feature_flag_manager: Any | None = None,
        media_integrity_validator: Any | None = None,
    ) -> None:
        if not callable(getattr(connection_manager, "verify_connectivity", None)):
            raise ValueError("connection_manager must support connectivity verification")
        expected = (
            (routing_manager, "route", "routing_manager"),
            (health_manager, "record_result", "health_manager"),
            (retry_policy, "decide", "retry_policy"),
            (output_validator, "validate_file", "output_validator"),
            (cost_policy, "reconcile_usage", "cost_policy"),
            (quota_manager, "commit", "quota_manager"),
            (credit_manager, "commit", "credit_manager"),
            (budget_alert_manager, "record", "budget_alert_manager"),
            (authorization_manager, "authorize", "authorization_manager"),
            (degraded_mode_manager, "evaluate", "degraded_mode_manager"),
            (sensitive_data_policy, "inspect", "sensitive_data_policy"),
            (data_handling_policy, "evaluate", "data_handling_policy"),
            (webhook_verifier, "verify", "webhook_verifier"),
            (job_manager, "submit", "job_manager"),
            (metrics_manager, "record", "metrics_manager"),
            (quality_policy, "evaluate", "quality_policy"),
            (benchmark_manager, "record", "benchmark_manager"),
            (incident_manager, "record", "incident_manager"),
            (feature_flag_manager, "evaluate", "feature_flag_manager"),
            (media_integrity_validator, "validate", "media_integrity_validator"),
        )
        for instance, method, label in expected:
            if instance is not None and not callable(getattr(instance, method, None)):
                raise ValueError(f"{label} does not support {method}")
        self.connection_manager = connection_manager
        self.routing_manager = routing_manager
        self.health_manager = health_manager
        self.retry_policy = retry_policy
        self.output_validator = output_validator
        self.cost_policy = cost_policy
        self.quota_manager = quota_manager
        self.credit_manager = credit_manager or getattr(routing_manager, "credit_manager", None)
        self.budget_alert_manager = budget_alert_manager
        self.authorization_manager = authorization_manager
        self.degraded_mode_manager = degraded_mode_manager
        self.sensitive_data_policy = sensitive_data_policy
        self.data_handling_policy = data_handling_policy
        self.webhook_verifier = webhook_verifier
        self.job_manager = job_manager
        self.metrics_manager = metrics_manager
        self.quality_policy = quality_policy
        self.benchmark_manager = benchmark_manager
        self.incident_manager = incident_manager
        self.feature_flag_manager = feature_flag_manager
        self.media_integrity_validator = media_integrity_validator

    def _authorize_operation(self, principal_id: str | None, operation: str) -> dict[str, Any] | None:
        if self.authorization_manager is None:
            return None
        try:
            result = self.authorization_manager.authorize(principal_id, operation)
        except Exception as exc:
            return {"allowed": False, "status": "AUTHORIZATION_CHECK_FAILED", "error_type": type(exc).__name__}
        if not isinstance(result, dict) or result.get("allowed") is not True:
            return result if isinstance(result, dict) else {
                "allowed": False, "status": "INVALID_AUTHORIZATION_RESPONSE"
            }
        return None

    def verify_provider(
        self,
        provider_id: str,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
        environ: dict[str, str] | None = None,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify connection using an injected provider-specific probe and record health."""
        authorization = self._authorize_operation(principal_id, "provider.verify")
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED", "provider": provider_id,
                "verified": False, "authorization": authorization,
            }
        try:
            connection = self.connection_manager.verify_connectivity(
                provider_id, probe, timeout_seconds=timeout_seconds, environ=environ
            )
        except Exception as exc:
            connection = {
                "status": "CONNECTION_MANAGER_FAILED",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        health: dict[str, Any] | None = None
        if self.health_manager is not None and isinstance(provider_id, str) and provider_id.strip():
            try:
                health = self.health_manager.record_result(
                    provider_id,
                    success=connection.get("verified") is True,
                    latency_ms=connection.get("latency_ms"),
                    error_code=None if connection.get("verified") is True else str(connection.get("status", "PROBE_FAILED")),
                )
            except Exception as exc:
                health = {"status": "HEALTH_RECORD_FAILED", "saved": False, "error_type": type(exc).__name__}
        return {
            "status": connection.get("status", "UNKNOWN"),
            "provider": provider_id,
            "verified": connection.get("verified") is True,
            "connection": connection,
            "health": health,
        }

    def verify_http_provider(
        self,
        provider_id: str,
        *,
        timeout_seconds: float = 5.0,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        max_response_bytes: int = 4096,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a configured allowlisted HTTPS endpoint using the built-in safe probe.

        This checks reachability and authentication only; it does not certify model
        support, account credits, generation quality, or provider-specific capabilities.
        """
        authorization = self._authorize_operation(principal_id, "provider.verify")
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED", "provider": provider_id,
                "verified": False, "authorization": authorization,
            }
        try:
            from .http_probe import ProviderHTTPConnectivityProbe

            probe = ProviderHTTPConnectivityProbe(
                environ=environ,
                opener=opener,
                max_response_bytes=max_response_bytes,
            )
        except Exception as exc:
            return {
                "status": "HTTP_PROBE_CONFIGURATION_INVALID",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        return self.verify_provider(
            provider_id,
            probe,
            timeout_seconds=timeout_seconds,
            environ=environ,
            principal_id=principal_id,
        )

    def verify_oauth_provider(
        self,
        provider_id: str,
        oauth_manager: Any,
        *,
        timeout_seconds: float = 5.0,
        environ: dict[str, str] | None = None,
        opener: Any = None,
        max_response_bytes: int = 4096,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a provider using an access token held only by an OAuth manager in memory.

        The access token is passed to the probe through an ephemeral environment mapping;
        it is never written to connection state, health history, or this result.
        Token refresh is explicit: call the OAuth manager's refresh method only when the
        provider supports it and the user-authorized session allows it.
        """
        authorization = self._authorize_operation(principal_id, "provider.verify")
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED", "provider": provider_id,
                "verified": False, "authorization": authorization,
            }
        if not callable(getattr(oauth_manager, "get_access_token", None)):
            return {"status": "OAUTH_MANAGER_INVALID", "provider": provider_id, "verified": False}
        try:
            provider = self.connection_manager.get_provider(provider_id)
        except Exception as exc:
            return {
                "status": "PROVIDER_LOOKUP_FAILED",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(provider, dict):
            return {"status": "NOT_FOUND", "provider": provider_id, "verified": False}
        token_env = provider.get("token_env") or provider.get("api_key_env") or provider.get("credential_env")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", token_env):
            return {"status": "OAUTH_TOKEN_ENV_REFERENCE_INVALID", "provider": provider_id, "verified": False}
        try:
            access_token = oauth_manager.get_access_token(provider_id)
        except Exception as exc:
            return {
                "status": "OAUTH_TOKEN_LOOKUP_FAILED",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(access_token, str) or not access_token.strip() or len(access_token) > 16384 or any(
            ord(char) < 32 or ord(char) == 127 for char in access_token
        ):
            return {"status": "OAUTH_ACCESS_TOKEN_UNAVAILABLE", "provider": provider_id, "verified": False}
        if environ is not None and not isinstance(environ, dict):
            return {"status": "INVALID_ENVIRONMENT", "provider": provider_id, "verified": False}
        ephemeral_environment = dict(environ or {})
        ephemeral_environment[token_env] = access_token
        return self.verify_http_provider(
            provider_id,
            timeout_seconds=timeout_seconds,
            environ=ephemeral_environment,
            opener=opener,
            max_response_bytes=max_response_bytes,
            principal_id=principal_id,
        )

    def verify_manual_provider(
        self,
        provider_id: str,
        manual_session: Any,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify an externally authorized account session and record safe health metadata."""
        authorization = self._authorize_operation(principal_id, "provider.verify")
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED", "provider": provider_id,
                "verified": False, "authorization": authorization,
            }
        try:
            connection = self.connection_manager.verify_manual_connection(
                provider_id, manual_session, probe, timeout_seconds=timeout_seconds
            )
        except Exception as exc:
            connection = {
                "status": "MANUAL_CONNECTION_FAILED",
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
            }
        health: dict[str, Any] | None = None
        if self.health_manager is not None and isinstance(provider_id, str) and provider_id.strip():
            try:
                health = self.health_manager.record_result(
                    provider_id,
                    success=connection.get("verified") is True,
                    latency_ms=connection.get("latency_ms"),
                    error_code=None if connection.get("verified") is True else str(connection.get("status", "MANUAL_CONNECTION_FAILED")),
                )
            except Exception as exc:
                health = {
                    "status": "HEALTH_RECORD_FAILED", "saved": False,
                    "error_type": type(exc).__name__,
                }
        return {
            "status": connection.get("status", "UNKNOWN"),
            "provider": provider_id,
            "verified": connection.get("verified") is True,
            "connection": connection,
            "health": health,
        }

    def submit_job(
        self, *, job_id: str, project_id: str, provider_id: str,
        payload_ref: str, priority: int = 50, principal_id: str | None = None,
    ) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "accepted": False}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "accepted": False, "authorization": authorization}
        try:
            result = self.job_manager.submit(
                job_id=job_id, project_id=project_id, provider_id=provider_id,
                payload_ref=payload_ref, priority=priority,
            )
        except Exception as exc:
            return {"status": "JOB_SUBMISSION_FAILED", "accepted": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "accepted": False}

    def poll_job(self, *, job_id: str, principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "job": None}
        authorization = self._authorize_operation(principal_id, "provider.read")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "job": None, "authorization": authorization}
        try:
            result = self.job_manager.poll(job_id=job_id)
        except Exception as exc:
            return {"status": "JOB_POLL_FAILED", "job": None, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "job": None}

    def claim_next_job(self, *, worker_id: str, principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "job": None}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "job": None, "authorization": authorization}
        try:
            result = self.job_manager.claim_next(worker_id=worker_id)
        except Exception as exc:
            return {"status": "JOB_CLAIM_FAILED", "job": None, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "job": None}

    def complete_job(self, *, job_id: str, worker_id: str, success: bool,
                     result_ref: str | None = None, error_code: str | None = None,
                     principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        try:
            result = self.job_manager.complete(
                job_id=job_id, worker_id=worker_id, success=success,
                result_ref=result_ref, error_code=error_code,
            )
        except Exception as exc:
            return {"status": "JOB_COMPLETION_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "saved": False}

    def cancel_job(self, *, job_id: str, principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        try:
            result = self.job_manager.cancel(job_id=job_id)
        except Exception as exc:
            return {"status": "JOB_CANCELLATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "saved": False}

    def verify_webhook(
        self,
        *,
        provider_id: str,
        payload: bytes,
        signature: str,
        timestamp: int | str,
        event_id: str,
        secret: str,
    ) -> dict[str, Any]:
        """Verify a provider callback without persisting its secret or body."""
        if self.webhook_verifier is None:
            return {"verified": False, "status": "WEBHOOK_VERIFIER_UNAVAILABLE"}
        try:
            result = self.webhook_verifier.verify(
                provider_id=provider_id,
                payload=payload,
                signature=signature,
                timestamp=timestamp,
                event_id=event_id,
                secret=secret,
            )
        except Exception as exc:
            return {
                "verified": False,
                "status": "WEBHOOK_VERIFICATION_FAILED",
                "error_type": type(exc).__name__,
            }
        return result if isinstance(result, dict) else {
            "verified": False, "status": "INVALID_WEBHOOK_VERIFIER_RESPONSE"
        }

    def route_request(self, **route_options: Any) -> dict[str, Any]:
        if self.routing_manager is None:
            return {"allowed": False, "status": "ROUTING_MANAGER_UNAVAILABLE"}
        try:
            result = self.routing_manager.route(**route_options)
        except Exception as exc:
            return {"allowed": False, "status": "ROUTING_FAILED", "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "allowed": False, "status": "INVALID_ROUTING_RESPONSE"
        }

    def validate_output(self, path: Any, *, modality: str) -> dict[str, Any]:
        if self.output_validator is None:
            return {"valid": False, "status": "OUTPUT_VALIDATOR_UNAVAILABLE"}
        try:
            result = self.output_validator.validate_file(path, modality=modality)
        except Exception as exc:
            return {"valid": False, "status": "OUTPUT_VALIDATION_FAILED", "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "valid": False, "status": "INVALID_OUTPUT_VALIDATION_RESPONSE"
        }

    @staticmethod
    def _safe_error_code(value: Any, default: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
            return default
        lowered = value.casefold()
        if any(term in lowered for term in ("token", "secret", "password", "credential", "authorization", "bearer")):
            return default
        return value

    def execute_routed_request(
        self,
        *,
        category: str,
        project_id: str,
        request_id: str,
        estimated_cost: float,
        adapter: Any,
        modality: str,
        principal_id: str | None = None,
        degraded_service_id: str | None = None,
        data_categories: tuple[str, ...] | list[str] = ("user_content",),
        retention_days: int = 0,
        provider_training_use: bool = False,
        user_training_consent: bool = False,
        output_path: Any = None,
        estimated_cost_unit: str = "USD",
        adapter_options: dict[str, Any] | None = None,
        **route_options: Any,
    ) -> dict[str, Any]:
        """Route and invoke one adapter attempt, validate output, then account safely.

        This method deliberately does not auto-retry: after an adapter exception, it
        may be impossible to know whether a paid/credit-consuming request started.
        """
        if not callable(adapter):
            return {"status": "INVALID_ADAPTER", "executed": False}
        if not isinstance(adapter_options, (dict, type(None))):
            return {"status": "INVALID_ADAPTER_OPTIONS", "executed": False}
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        if not isinstance(category, str) or not category.strip():
            return {"status": "INVALID_CATEGORY", "executed": False}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"status": "INVALID_PROJECT_ID", "executed": False}
        if not self._valid_amount(estimated_cost):
            return {"status": "INVALID_COST_AMOUNT", "executed": False}
        degraded_mode_result = None
        if self.degraded_mode_manager is not None:
            service_scope = degraded_service_id or category
            if not isinstance(service_scope, str) or not service_scope.strip():
                return {"status": "DEGRADED_SERVICE_SCOPE_REQUIRED", "executed": False}
            try:
                degraded_mode_result = self.degraded_mode_manager.evaluate(
                    project_id=project_id, service_id=service_scope,
                    operation="provider.generate",
                )
            except Exception as exc:
                return {"status": "DEGRADED_MODE_CHECK_FAILED", "executed": False, "error_type": type(exc).__name__}
            if not isinstance(degraded_mode_result, dict) or degraded_mode_result.get("allowed") is not True:
                return {"status": "DEGRADED_MODE_BLOCKED", "executed": False, "degraded_mode": degraded_mode_result}

        feature_flag_result = None
        if self.feature_flag_manager is not None:
            try:
                feature_flag_result = self.feature_flag_manager.evaluate(
                    "provider_generation", project_id=project_id, request_id=request_id
                )
            except Exception as exc:
                return {
                    "status": "FEATURE_FLAG_CHECK_FAILED",
                    "executed": False,
                    "error_type": type(exc).__name__,
                }
            if not isinstance(feature_flag_result, dict) or feature_flag_result.get("enabled") is not True:
                return {
                    "status": "FEATURE_DISABLED",
                    "executed": False,
                    "feature_flag": feature_flag_result if isinstance(feature_flag_result, dict) else {
                        "enabled": False, "status": "INVALID_FEATURE_FLAG_RESPONSE"
                    },
                }

        authorization = None
        if self.authorization_manager is not None:
            try:
                authorization = self.authorization_manager.authorize(principal_id, "provider.generate")
            except Exception as exc:
                return {"status": "AUTHORIZATION_CHECK_FAILED", "executed": False, "error_type": type(exc).__name__}
            if not isinstance(authorization, dict) or authorization.get("allowed") is not True:
                return {
                    "status": "AUTHORIZATION_DENIED", "executed": False,
                    "authorization": authorization if isinstance(authorization, dict) else {
                        "allowed": False, "status": "INVALID_AUTHORIZATION_RESPONSE"
                    },
                }

        sensitive_data_scan = None
        if self.sensitive_data_policy is not None:
            try:
                sensitive_data_scan = self.sensitive_data_policy.inspect(adapter_options or {})
            except Exception as exc:
                return {"status": "SENSITIVE_DATA_SCAN_FAILED", "executed": False, "error_type": type(exc).__name__}
            if not isinstance(sensitive_data_scan, dict) or sensitive_data_scan.get("allowed") is not True:
                return {
                    "status": "SENSITIVE_DATA_BLOCKED", "executed": False,
                    "sensitive_data_scan": sensitive_data_scan if isinstance(sensitive_data_scan, dict) else {
                        "allowed": False, "status": "INVALID_SENSITIVE_DATA_SCAN_RESPONSE"
                    },
                }

        route = self.route_request(
            category=category,
            project_id=project_id,
            request_id=request_id,
            estimated_cost=estimated_cost,
            estimated_cost_unit=estimated_cost_unit,
            **route_options,
        )
        if not isinstance(route, dict) or route.get("allowed") is not True:
            return {"status": "ROUTING_REJECTED", "executed": False, "route": route}
        provider_id = route.get("provider")
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_ROUTE_PROVIDER", "executed": False, "route": route}
        if self.degraded_mode_manager is not None:
            try:
                provider_mode = self.degraded_mode_manager.evaluate(
                    project_id=project_id, service_id=degraded_service_id or category,
                    operation="provider.generate", provider_id=provider_id,
                )
            except Exception as exc:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id
                )
                return {
                    "status": "DEGRADED_MODE_CHECK_FAILED", "executed": False,
                    "release": release, "error_type": type(exc).__name__,
                }
            if not isinstance(provider_mode, dict) or provider_mode.get("allowed") is not True:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id
                )
                return {
                    "status": "DEGRADED_MODE_BLOCKED", "executed": False,
                    "degraded_mode": provider_mode, "release": release,
                }
            degraded_mode_result = provider_mode

        data_policy_result = None
        if self.data_handling_policy is not None:
            try:
                data_policy_result = self.data_handling_policy.evaluate(
                    provider_id=provider_id,
                    data_categories=data_categories,
                    retention_days=retention_days,
                    provider_training_use=provider_training_use,
                    user_training_consent=user_training_consent,
                )
            except Exception as exc:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id
                )
                return {
                    "status": "PROVIDER_DATA_POLICY_CHECK_FAILED", "executed": False,
                    "release": release, "error_type": type(exc).__name__,
                }
            if not isinstance(data_policy_result, dict) or data_policy_result.get("allowed") is not True:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id
                )
                return {
                    "status": "PROVIDER_DATA_POLICY_REJECTED", "executed": False,
                    "data_policy": data_policy_result if isinstance(data_policy_result, dict) else {
                        "allowed": False, "status": "INVALID_DATA_POLICY_RESPONSE"
                    },
                    "release": release,
                }

        options = dict(adapter_options or {})
        options["provider_id"] = provider_id
        options["request_id"] = request_id
        started_at = time.monotonic()
        try:
            adapter_result = adapter(**options)
        except Exception as exc:
            # The adapter may have transmitted the request before failing. Do not
            # release cost, quota, or credit reservations on this ambiguous outcome.
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                "reservations_retained": True,
                "error_type": type(exc).__name__,
            }
        if not isinstance(adapter_result, dict) or not isinstance(adapter_result.get("started"), bool):
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                "reservations_retained": True,
            }
        if adapter_result["started"] is False:
            if (
                adapter_result.get("success") is True
                or (
                    self._valid_amount(adapter_result.get("actual_cost"))
                    and adapter_result.get("actual_cost") > 0
                )
                or adapter_result.get("charge_outcome") == "incurred"
                or adapter_result.get("credit_outcome") == "used"
                or (
                    self._valid_amount(adapter_result.get("actual_credit_amount"))
                    and adapter_result.get("actual_credit_amount") > 0
                )
            ):
                return {
                    "status": "INCONSISTENT_START_RESULT",
                    "executed": False,
                    "provider": provider_id,
                    "request_id": request_id,
                    "reservations_retained": True,
                }
            released = self.release_unstarted_request(
                project_id=project_id, provider_id=provider_id, request_id=request_id
            )
            return {
                "status": "EXECUTION_NOT_STARTED" if released.get("released") is True else "RESERVATION_RELEASE_FAILED",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                "release": released,
            }

        accounting_required = False
        reported_success = adapter_result.get("success")
        if not isinstance(reported_success, bool):
            reported_success = False
            accounting_required = True
        actual_cost = adapter_result.get("actual_cost")
        actual_cost_valid = self._valid_amount(actual_cost)
        if not actual_cost_valid:
            actual_cost = 0.0
            accounting_required = True
        charge_outcome = adapter_result.get("charge_outcome", "unknown")
        if (
            not actual_cost_valid
            or not isinstance(charge_outcome, str)
            or charge_outcome not in {"unknown", "incurred", "not_incurred"}
        ):
            charge_outcome = "unknown"
            accounting_required = True
        if actual_cost > 0 and charge_outcome == "not_incurred":
            charge_outcome = "unknown"
            accounting_required = True

        credit_outcome = adapter_result.get("credit_outcome", "unknown")
        if not isinstance(credit_outcome, str) or credit_outcome not in {"unknown", "used", "not_used"}:
            credit_outcome = "unknown"
            accounting_required = True
        actual_credit_amount = adapter_result.get("actual_credit_amount")
        if actual_credit_amount is not None and not self._valid_amount(actual_credit_amount):
            actual_credit_amount = None
            credit_outcome = "unknown"
            accounting_required = True

        latency_ms = adapter_result.get("latency_ms")
        if not self._valid_amount(latency_ms):
            latency_ms = (time.monotonic() - started_at) * 1000
        output_validation = None
        media_integrity_result = None
        quality_result = None
        quality_error = None
        final_success = reported_success
        output_error = None
        if reported_success:
            final_path = adapter_result.get("output_path", output_path)
            if final_path is None:
                output_validation = {"valid": False, "status": "OUTPUT_PATH_REQUIRED"}
            else:
                output_validation = self.validate_output(final_path, modality=modality)
            if (
                isinstance(output_validation, dict)
                and output_validation.get("valid") is True
                and modality in {"audio", "video"}
                and self.media_integrity_validator is not None
            ):
                try:
                    media_integrity_result = self.media_integrity_validator.validate(
                        final_path, modality=modality
                    )
                except Exception as exc:
                    media_integrity_result = {
                        "valid": False, "status": "MEDIA_INTEGRITY_CHECK_FAILED",
                        "error_type": type(exc).__name__,
                    }
                if not isinstance(media_integrity_result, dict) or media_integrity_result.get("valid") is not True:
                    output_validation = {
                        "valid": False,
                        "status": "MEDIA_INTEGRITY_VALIDATION_FAILED",
                    }
            if not isinstance(output_validation, dict) or output_validation.get("valid") is not True:
                final_success = False
                output_error = "OUTPUT_VALIDATION_FAILED"
            elif self.quality_policy is not None:
                try:
                    quality_result = self.quality_policy.evaluate(
                        final_path, modality=modality, category=category
                    )
                except Exception as exc:
                    quality_result = {
                        "allowed": False, "status": "QUALITY_SCORING_FAILED",
                        "error_type": type(exc).__name__,
                    }
                if not isinstance(quality_result, dict) or quality_result.get("allowed") is not True:
                    final_success = False
                    quality_error = (
                        quality_result.get("status")
                        if isinstance(quality_result, dict)
                        else "INVALID_QUALITY_POLICY_RESPONSE"
                    )

        error_code = self._safe_error_code(
            adapter_result.get("error_code"),
            quality_error or output_error or ("PROVIDER_EXECUTION_FAILED" if not final_success else "PROVIDER_EXECUTION_SUCCEEDED"),
        )
        finalization = self.finalize_started_request(
            project_id=project_id,
            provider_id=provider_id,
            request_id=request_id,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            success=final_success,
            latency_ms=latency_ms,
            error_code=None if final_success else error_code,
            charge_outcome=charge_outcome,
            billing_mode=route.get("billing_mode"),
            credit_outcome=credit_outcome,
            actual_credit_amount=actual_credit_amount,
        )
        benchmark_result = None
        if self.benchmark_manager is not None:
            model_id = adapter_result.get("model_id", route.get("model_id"))
            if not isinstance(model_id, str) or not model_id.strip():
                benchmark_result = {"status": "MODEL_ID_UNAVAILABLE", "saved": False}
            else:
                try:
                    benchmark_result = self.benchmark_manager.record(
                        provider_id=provider_id,
                        model_id=model_id,
                        modality=modality,
                        run_id=adapter_result.get("benchmark_run_id", request_id),
                        success=final_success,
                        latency_ms=latency_ms,
                        actual_cost=actual_cost,
                        quality_score=quality_result.get("score") if isinstance(quality_result, dict) else None,
                    )
                except Exception as exc:
                    benchmark_result = {
                        "status": "BENCHMARK_RECORD_FAILED",
                        "saved": False,
                        "error_type": type(exc).__name__,
                    }
        metrics_result = None
        if self.metrics_manager is not None:
            try:
                metrics_result = self.metrics_manager.record(
                    provider_id=provider_id,
                    category=category,
                    success=final_success,
                    latency_ms=latency_ms,
                    queue_wait_ms=adapter_result.get("queue_wait_ms", 0.0),
                    timed_out=adapter_result.get("status_code") in {408, 504},
                )
            except Exception as exc:
                metrics_result = {
                    "status": "METRICS_RECORD_FAILED",
                    "saved": False,
                    "error_type": type(exc).__name__,
                }
        budget_alerts = None
        if self.budget_alert_manager is not None and self.cost_policy is not None:
            try:
                budgets = getattr(self.cost_policy, "project_budgets", {})
                budget = budgets.get(project_id) if isinstance(budgets, dict) else None
                ledger = getattr(self.cost_policy, "ledger", {})
                projects = ledger.get("projects", {}) if isinstance(ledger, dict) else {}
                project = projects.get(project_id, {}) if isinstance(projects, dict) else {}
                reservations = project.get("reservations", {}) if isinstance(project, dict) else {}
                reserved = sum(
                    float(item.get("estimated_cost", 0.0))
                    for item in reservations.values()
                    if isinstance(item, dict) and self._valid_amount(item.get("estimated_cost"))
                ) if isinstance(reservations, dict) else 0.0
                spend = float(project.get("actual_spend", 0.0)) + reserved if isinstance(project, dict) else reserved
                if self._valid_amount(budget) and budget > 0:
                    budget_alerts = self.budget_alert_manager.record(
                        project_id=project_id, spend=spend, budget=budget, request_id=request_id
                    )
                else:
                    budget_alerts = {"status": "PROJECT_BUDGET_UNAVAILABLE", "alerts": []}
            except Exception as exc:
                budget_alerts = {
                    "status": "BUDGET_ALERT_RECORD_FAILED",
                    "error_type": type(exc).__name__, "alerts": [],
                }
        retry = None
        provider_error = None
        status_code = adapter_result.get("status_code")
        provider_code = adapter_result.get("provider_error_code", adapter_result.get("error_code"))
        if not final_success and status_code is not None:
            provider_error = classify_provider_error(
                status_code,
                provider_code=provider_code,
                retry_after_seconds=adapter_result.get("retry_after_seconds"),
            )
        if not final_success and self.retry_policy is not None:
            if provider_error is not None and provider_error.get("retryable") is False:
                # Billing, exhausted-credit, authentication, permission and invalid
                # request errors must not be retried just because the retry policy
                # sees a status such as 429.
                retry = {"retry": False, "reason": provider_error["status"]}
            else:
                retry = self.retry_decision(
                    attempt=adapter_result.get("attempt", 1),
                    status_code=adapter_result.get("status_code"),
                    retry_after=adapter_result.get("retry_after"),
                    idempotent=adapter_result.get("idempotent", False),
                )
        incident_result = None
        if self.incident_manager is not None and not final_success:
            if isinstance(provider_error, dict) and provider_error.get("category") in {"billing", "credits_exhausted"}:
                severity = "critical"
            elif isinstance(provider_error, dict) and provider_error.get("category") == "transient":
                severity = "warning"
            else:
                severity = "error"
            incident_code = (
                provider_error.get("status") if isinstance(provider_error, dict)
                else quality_error or output_error or error_code
            )
            try:
                incident_result = self.incident_manager.record(
                    provider_id=provider_id,
                    category=category,
                    severity=severity,
                    code=incident_code,
                )
            except Exception as exc:
                incident_result = {
                    "status": "INCIDENT_RECORD_FAILED",
                    "saved": False,
                    "error_type": type(exc).__name__,
                }
        if accounting_required:
            status = "EXECUTION_ACCOUNTING_REQUIRED"
        elif quality_error is not None:
            status = "OUTPUT_QUALITY_FAILED"
        elif output_error is not None:
            status = "OUTPUT_VALIDATION_FAILED"
        elif not final_success:
            status = "EXECUTION_FAILED"
        elif finalization.get("finalized") is True:
            status = "EXECUTION_SUCCEEDED"
        else:
            status = "EXECUTION_FINALIZATION_PARTIAL"
        return {
            "status": status,
            "executed": True,
            "provider": provider_id,
            "request_id": request_id,
            "billing_mode": route.get("billing_mode"),
            "output": output_validation,
            "media_integrity": media_integrity_result,
            "quality": quality_result,
            "finalization": finalization,
            "metrics": metrics_result,
            "benchmark": benchmark_result,
            "incident": incident_result,
            "provider_error": provider_error,
            "budget_alerts": budget_alerts,
            "authorization": authorization,
            "feature_flag": feature_flag_result,
            "degraded_mode": degraded_mode_result,
            "sensitive_data_scan": sensitive_data_scan,
            "data_policy": data_policy_result,
            "retry_decision": retry,
        }

    def retry_decision(self, **failure: Any) -> dict[str, Any]:
        if self.retry_policy is None:
            return {"retry": False, "reason": "RETRY_POLICY_UNAVAILABLE"}
        try:
            decision = self.retry_policy.decide(**failure)
        except Exception as exc:
            return {"retry": False, "reason": "RETRY_POLICY_FAILED", "error_type": type(exc).__name__}
        return {
            "retry": bool(getattr(decision, "retry", False)),
            "attempt": getattr(decision, "attempt", 0),
            "delay_seconds": getattr(decision, "delay_seconds", 0.0),
            "reason": getattr(decision, "reason", "INVALID_RETRY_RESPONSE"),
        }

    @staticmethod
    def _valid_amount(value: Any) -> bool:
        return finite_number(value, minimum=0) is not None

    def finalize_started_request(
        self,
        *,
        project_id: str,
        provider_id: str,
        request_id: str,
        estimated_cost: float,
        actual_cost: float,
        success: bool,
        latency_ms: float | None = None,
        error_code: str | None = None,
        charge_outcome: str = "unknown",
        billing_mode: str | None = None,
        credit_outcome: str = "used",
        actual_credit_amount: float | None = None,
    ) -> dict[str, Any]:
        """Account for a started request without assuming ambiguous failures were free.

        charge_outcome is consulted when execution failed with zero reported cost:
        not_incurred must be an explicit provider-confirmed no-charge outcome;
        unknown retains the cost reservation; incurred flags missing usage data.
        Quota is committed for every started request, including failed requests.
        """
        if not all(isinstance(value, str) and value.strip() for value in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "finalized": False}
        if not isinstance(success, bool):
            return {"status": "INVALID_EXECUTION_RESULT", "finalized": False}
        if not isinstance(charge_outcome, str) or charge_outcome not in {"unknown", "incurred", "not_incurred"}:
            return {"status": "INVALID_CHARGE_OUTCOME", "finalized": False}
        if billing_mode is not None and (
            not isinstance(billing_mode, str)
            or billing_mode not in {"ZERO_COST", "FREE_CREDITS", "PAID_APPROVED"}
        ):
            return {"status": "INVALID_BILLING_MODE", "finalized": False}
        if not isinstance(credit_outcome, str) or credit_outcome not in {"unknown", "used", "not_used"}:
            return {"status": "INVALID_CREDIT_OUTCOME", "finalized": False}
        if actual_credit_amount is not None and not self._valid_amount(actual_credit_amount):
            return {"status": "INVALID_CREDIT_AMOUNT", "finalized": False}
        if not self._valid_amount(estimated_cost) or not self._valid_amount(actual_cost):
            return {"status": "INVALID_COST_AMOUNT", "finalized": False}
        if actual_cost > 0 and charge_outcome == "not_incurred":
            return {"status": "INCONSISTENT_CHARGE_OUTCOME", "finalized": False}
        if latency_ms is not None and not self._valid_amount(latency_ms):
            return {"status": "INVALID_LATENCY", "finalized": False}
        cost_result: dict[str, Any] | None = None
        quota_result: dict[str, Any] | None = None
        credit_result: dict[str, Any] | None = None
        duplicate_finalization = False
        atomic_finalize = (
            getattr(self.cost_policy, "finalize_usage", None)
            if self.cost_policy is not None else None
        )
        if callable(atomic_finalize):
            try:
                cost_result = atomic_finalize(
                    project_id=project_id,
                    provider_id=provider_id,
                    estimated_cost=estimated_cost,
                    actual_cost=actual_cost,
                    request_id=request_id,
                    success=success,
                    charge_outcome=charge_outcome,
                )
            except Exception as exc:
                cost_result = {
                    "status": "COST_RECONCILIATION_FAILED",
                    "saved": False,
                    "error_type": type(exc).__name__,
                }
        if self.cost_policy is not None and not callable(atomic_finalize):
            ledger = getattr(self.cost_policy, "ledger", {})
            projects = ledger.get("projects", {}) if isinstance(ledger, dict) else {}
            project = projects.get(project_id, {}) if isinstance(projects, dict) else {}
            requests = project.get("requests", {}) if isinstance(project, dict) else {}
            existing = requests.get(request_id) if isinstance(requests, dict) else None
            if isinstance(existing, dict):
                duplicate_finalization = True
                if (
                    existing.get("provider_id") != provider_id
                    or existing.get("estimated_cost") != float(estimated_cost)
                    or existing.get("actual_cost") != float(actual_cost)
                    or existing.get("execution_success", success) != success
                    or existing.get("charge_outcome", "unknown") != charge_outcome
                ):
                    cost_result = {"status": "REQUEST_ID_CONFLICT", "saved": False}
                else:
                    cost_result = {"status": "DUPLICATE_REQUEST", "saved": True}
            elif estimated_cost > 0 and actual_cost == 0:
                # A zero actual-cost report is not proof of no charge, even if the
                # generation succeeded. Only explicit provider confirmation closes it.
                try:
                    if charge_outcome == "not_incurred":
                        record_no_charge = getattr(self.cost_policy, "record_no_charge", None)
                        if not callable(record_no_charge):
                            cost_result = {"status": "NO_CHARGE_LEDGER_UNAVAILABLE", "saved": False}
                        else:
                            cost_result = record_no_charge(
                                project_id=project_id,
                                provider_id=provider_id,
                                estimated_cost=estimated_cost,
                                request_id=request_id,
                                success=success,
                            )
                    else:
                        release = getattr(self.cost_policy, "release_reservation", None)
                        if not callable(release):
                            cost_result = {"status": "CHARGE_OUTCOME_UNKNOWN", "saved": False}
                        else:
                            cost_result = release(
                                project_id=project_id,
                                request_id=request_id,
                                provider_id=provider_id,
                                charge_outcome=charge_outcome,
                            )
                            if charge_outcome == "unknown":
                                cost_result = {**cost_result, "status": "CHARGE_OUTCOME_UNKNOWN", "saved": False}
                            elif charge_outcome == "incurred":
                                cost_result = {**cost_result, "status": "RECONCILIATION_REQUIRED", "saved": False}
                except Exception as exc:
                    cost_result = {"status": "COST_RECONCILIATION_FAILED", "saved": False, "error_type": type(exc).__name__}
            else:
                try:
                    cost_result = self.cost_policy.reconcile_usage(
                        project_id=project_id,
                        provider_id=provider_id,
                        estimated_cost=estimated_cost,
                        actual_cost=actual_cost,
                        request_id=request_id,
                        success=success,
                        charge_outcome=charge_outcome,
                    )
                    if (
                        isinstance(cost_result, dict)
                        and cost_result.get("status") == "DUPLICATE_REQUEST"
                        and cost_result.get("saved") is True
                    ):
                        duplicate_finalization = True
                except Exception as exc:
                    cost_result = {"status": "COST_RECONCILIATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        if billing_mode == "FREE_CREDITS":
            if self.credit_manager is None:
                credit_result = {"status": "CREDIT_MANAGER_UNAVAILABLE", "saved": False}
            elif credit_outcome == "unknown":
                credit_result = {"status": "CREDIT_OUTCOME_UNKNOWN", "saved": False}
            elif credit_outcome == "not_used":
                try:
                    released_credit = self.credit_manager.release(
                        request_id=request_id, provider_id=provider_id
                    )
                    credit_result = {
                        **released_credit,
                        "saved": released_credit.get("released") is True
                        or released_credit.get("status") == "RESERVATION_NOT_FOUND",
                    }
                except Exception as exc:
                    credit_result = {"status": "CREDIT_RELEASE_FAILED", "saved": False, "error_type": type(exc).__name__}
            else:
                try:
                    committed_credit = self.credit_manager.commit(
                        request_id=request_id, actual_amount=actual_credit_amount
                    )
                    credit_result = {
                        **committed_credit,
                        "saved": committed_credit.get("committed") is True
                        or committed_credit.get("status") == "ALREADY_COMMITTED",
                    }
                    if (
                        credit_result.get("status") == "ALREADY_COMMITTED"
                        and (cost_result is None or cost_result.get("saved") is not True)
                    ):
                        duplicate_finalization = True
                except Exception as exc:
                    credit_result = {"status": "CREDIT_COMMIT_FAILED", "saved": False, "error_type": type(exc).__name__}
        if (
            isinstance(cost_result, dict)
            and cost_result.get("status") == "DUPLICATE_REQUEST"
            and cost_result.get("saved") is True
        ):
            duplicate_finalization = True
        if self.quota_manager is not None:
            try:
                quota_result = self.quota_manager.commit(request_id=request_id)
                if (
                    isinstance(quota_result, dict)
                    and quota_result.get("status") == "ALREADY_COMMITTED"
                    and (cost_result is None or cost_result.get("saved") is not True)
                ):
                    # Quota may win the race before the first cost-ledger writer reaches
                    # this point. A newly saved cost record still owns the health event.
                    duplicate_finalization = True
            except Exception as exc:
                quota_result = {"status": "QUOTA_COMMIT_FAILED", "committed": False, "error_type": type(exc).__name__}
        health_result: dict[str, Any] | None = None
        if self.health_manager is not None and duplicate_finalization:
            health_result = {"status": "SKIPPED_DUPLICATE_FINALIZATION", "saved": True}
        elif self.health_manager is not None:
            try:
                health_result = self.health_manager.record_result(
                    provider_id,
                    success=success,
                    latency_ms=latency_ms,
                    error_code=None if success else (error_code or "EXECUTION_FAILED"),
                    request_id=request_id,
                )
            except Exception as exc:
                health_result = {"status": "HEALTH_RECORD_FAILED", "saved": False, "error_type": type(exc).__name__}
        finalized = (
            (cost_result is None or cost_result.get("saved") is True)
            and (quota_result is None or quota_result.get("committed") is True or quota_result.get("status") == "ALREADY_COMMITTED")
            and (credit_result is None or credit_result.get("saved") is True)
            and (health_result is None or health_result.get("saved") is True)
        )
        return {
            "status": "FINALIZED" if finalized else "FINALIZATION_PARTIAL",
            "finalized": finalized,
            "cost": cost_result,
            "quota": quota_result,
            "credits": credit_result,
            "health": health_result,
        }

    def release_unstarted_request(
        self, *, project_id: str, provider_id: str, request_id: str
    ) -> dict[str, Any]:
        """Release reservations only when the caller knows no provider request started."""
        if not all(isinstance(value, str) and value.strip() for value in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "released": False}
        results: dict[str, Any] = {}
        if self.cost_policy is None and self.quota_manager is None and self.credit_manager is None:
            return {"status": "RESERVATION_MANAGERS_UNAVAILABLE", "released": False}
        if self.cost_policy is not None:
            release = getattr(self.cost_policy, "release_reservation", None)
            if callable(release):
                try:
                    results["cost"] = release(
                        project_id=project_id,
                        request_id=request_id,
                        provider_id=provider_id,
                        charge_outcome="not_incurred",
                    )
                except Exception as exc:
                    results["cost"] = {"status": "COST_RELEASE_FAILED", "error_type": type(exc).__name__}
        if self.quota_manager is not None:
            release = getattr(self.quota_manager, "release", None)
            if callable(release):
                try:
                    results["quota"] = release(request_id=request_id, provider_id=provider_id)
                except Exception as exc:
                    results["quota"] = {"status": "QUOTA_RELEASE_FAILED", "error_type": type(exc).__name__}
        if self.credit_manager is not None:
            release = getattr(self.credit_manager, "release", None)
            if callable(release):
                try:
                    results["credits"] = release(request_id=request_id, provider_id=provider_id)
                except Exception as exc:
                    results["credits"] = {"status": "CREDIT_RELEASE_FAILED", "error_type": type(exc).__name__}
        released = all(
            item.get("status") in {"RESERVATION_RELEASED", "RELEASED", "RESERVATION_NOT_FOUND"}
            for item in results.values()
        )
        return {"status": "RELEASED" if released else "RELEASE_PARTIAL", "released": released, **results}
