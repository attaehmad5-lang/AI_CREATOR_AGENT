"""Integration facade for provider connection, routing, health, cost, quota, and outputs."""
from __future__ import annotations

from typing import Any
from pathlib import Path
import re
import time

from .concurrency_manager import ProviderConcurrencyManager
from .execution_ledger import ProviderExecutionLedger
from .safe_numbers import finite_number
from .trace_context import TraceContext, TraceContextError


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
        execution_ledger: Any | None = None,
        concurrency_manager: Any | None = None,
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
            (execution_ledger, "reserve", "execution_ledger"),
            (execution_ledger, "mark_started", "execution_ledger"),
            (execution_ledger, "finish", "execution_ledger"),
            (concurrency_manager, "acquire", "concurrency_manager"),
            (concurrency_manager, "mark_started", "concurrency_manager"),
            (concurrency_manager, "release", "concurrency_manager"),
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
        self.concurrency_manager_error: str | None = None
        if concurrency_manager is not None:
            self.concurrency_manager = concurrency_manager
        else:
            state_path = getattr(connection_manager, "state_path", None)
            if isinstance(state_path, (str, Path)):
                try:
                    self.concurrency_manager = ProviderConcurrencyManager(
                        Path(state_path).with_name("provider_concurrency.json")
                    )
                except Exception as exc:
                    self.concurrency_manager = None
                    self.concurrency_manager_error = type(exc).__name__
            else:
                self.concurrency_manager = None
        self.execution_ledger_error: str | None = None
        if execution_ledger is not None:
            self.execution_ledger = execution_ledger
        else:
            state_path = getattr(connection_manager, "state_path", None)
            if isinstance(state_path, (str, Path)):
                try:
                    self.execution_ledger = ProviderExecutionLedger(
                        Path(state_path).with_name("provider_execution_ledger.json")
                    )
                except Exception as exc:
                    self.execution_ledger = None
                    self.execution_ledger_error = type(exc).__name__
            else:
                self.execution_ledger = None

    def verify_provider(
        self,
        provider_id: str,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
        environ: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Verify connection using an injected provider-specific probe and record health."""
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
    ) -> dict[str, Any]:
        """Verify a configured allowlisted HTTPS endpoint using the built-in safe probe.

        This checks reachability and authentication only; it does not certify model
        support, account credits, generation quality, or provider-specific capabilities.
        """
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
    ) -> dict[str, Any]:
        """Verify a provider using an access token held only by an OAuth manager in memory.

        The access token is passed to the probe through an ephemeral environment mapping;
        it is never written to connection state, health history, or this result.
        Token refresh is explicit: call the OAuth manager's refresh method only when the
        provider supports it and the user-authorized session allows it.
        """
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
        )

    def verify_manual_provider(
        self,
        provider_id: str,
        manual_session: Any,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Verify an externally authorized account session and record safe health metadata."""
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

    def _ledger_reserve(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.execution_ledger.reserve(**kwargs)
        except Exception as exc:
            return {"status": "EXECUTION_LEDGER_FAILED", "reserved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_EXECUTION_LEDGER_RESPONSE", "reserved": False,
        }

    def _ledger_mark_started(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.execution_ledger.mark_started(**kwargs)
        except Exception as exc:
            return {"status": "EXECUTION_LEDGER_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_EXECUTION_LEDGER_RESPONSE", "saved": False,
        }

    def _ledger_finish(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.execution_ledger.finish(**kwargs)
        except Exception as exc:
            return {"status": "EXECUTION_LEDGER_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_EXECUTION_LEDGER_RESPONSE", "saved": False,
        }

    def _concurrency_acquire(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.concurrency_manager.acquire(**kwargs)
        except Exception as exc:
            return {"status": "CONCURRENCY_MANAGER_FAILED", "acquired": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_CONCURRENCY_RESPONSE", "acquired": False,
        }

    def _concurrency_mark_started(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.concurrency_manager.mark_started(**kwargs)
        except Exception as exc:
            return {"status": "CONCURRENCY_MANAGER_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_CONCURRENCY_RESPONSE", "saved": False,
        }

    def _concurrency_release(self, **kwargs: Any) -> dict[str, Any]:
        try:
            result = self.concurrency_manager.release(**kwargs)
        except Exception as exc:
            return {"status": "CONCURRENCY_MANAGER_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_CONCURRENCY_RESPONSE", "saved": False,
        }

    def execute_routed_request(
        self,
        *,
        category: str,
        project_id: str,
        request_id: str | None = None,
        estimated_cost: float,
        adapter: Any,
        modality: str,
        output_path: Any = None,
        estimated_cost_unit: str = "USD",
        adapter_options: dict[str, Any] | None = None,
        trace_id: str | None = None,
        traceparent: str | None = None,
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
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id.strip()
        ):
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        if not isinstance(category, str) or not category.strip():
            return {"status": "INVALID_CATEGORY", "executed": False}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"status": "INVALID_PROJECT_ID", "executed": False}
        if not self._valid_amount(estimated_cost):
            return {"status": "INVALID_COST_AMOUNT", "executed": False}
        try:
            trace_context = TraceContext.create(
                request_id, trace_id=trace_id, traceparent=traceparent
            )
        except TraceContextError:
            return {"status": "INVALID_TRACE_CONTEXT", "executed": False}
        request_id = trace_context.request_id
        trace_fields = {
            "trace_id": trace_context.trace_id,
            "traceparent": trace_context.traceparent,
        }

        if self.execution_ledger is None:
            return {
                "status": "EXECUTION_LEDGER_UNAVAILABLE",
                "executed": False,
                "request_id": request_id,
                **trace_fields,
            }
        fingerprint_options: dict[str, Any] = {
            "modality": modality,
            "estimated_cost_unit": estimated_cost_unit,
        }
        for key in (
            "model", "model_version", "capability", "strategy",
            "automatic_fallback", "require_health_evidence",
        ):
            if key in route_options:
                fingerprint_options[key] = route_options[key]
        for key in ("paid_approved_providers", "excluded_providers"):
            value = route_options.get(key)
            if isinstance(value, (set, frozenset, list, tuple)):
                fingerprint_options[key] = value
            elif key in route_options:
                fingerprint_options[key] = value
        try:
            fingerprint = ProviderExecutionLedger.fingerprint_request(
                project_id=project_id,
                category=category,
                estimated_cost=estimated_cost,
                routing_options=fingerprint_options,
            )
        except (TypeError, ValueError):
            return {
                "status": "INVALID_REQUEST_FINGERPRINT",
                "executed": False,
                "request_id": request_id,
                **trace_fields,
            }
        claim = self._ledger_reserve(
            request_id=request_id,
            project_id=project_id,
            category=category,
            estimated_cost=estimated_cost,
            trace_id=trace_context.trace_id,
            fingerprint=fingerprint,
        )
        if not isinstance(claim, dict) or claim.get("reserved") is not True:
            claim_status = claim.get("status", "EXECUTION_LEDGER_FAILED") if isinstance(claim, dict) else "EXECUTION_LEDGER_FAILED"
            return {
                "status": claim_status,
                "executed": False,
                "duplicate": claim_status == "DUPLICATE_REQUEST",
                "request_id": request_id,
                "trace_id": claim.get("trace_id", trace_context.trace_id) if isinstance(claim, dict) else trace_context.trace_id,
                "previous_status": claim.get("existing_status") if isinstance(claim, dict) else None,
                "previous_result": claim.get("result_status") if isinstance(claim, dict) else None,
                "provider": claim.get("provider_id") if isinstance(claim, dict) else None,
                "safe_to_retry": False,
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
            finished = self._ledger_finish(
                request_id=request_id, status="REJECTED", result_status="ROUTING_REJECTED"
            )
            if not isinstance(finished, dict) or finished.get("saved") is not True:
                return {"status": "EXECUTION_LEDGER_FINISH_FAILED", "executed": False, "route": route, **trace_fields}
            return {"status": "ROUTING_REJECTED", "executed": False, "route": route, **trace_fields}
        provider_id = route.get("provider")
        if not isinstance(provider_id, str) or not provider_id.strip():
            self._ledger_finish(
                request_id=request_id, status="REJECTED", result_status="INVALID_ROUTE_PROVIDER"
            )
            return {"status": "INVALID_ROUTE_PROVIDER", "executed": False, "route": route, **trace_fields}

        started_claim = self._ledger_mark_started(
            request_id=request_id, provider_id=provider_id
        )
        if not isinstance(started_claim, dict) or started_claim.get("saved") is not True:
            released = self.release_unstarted_request(
                project_id=project_id, provider_id=provider_id, request_id=request_id
            )
            return {
                "status": "EXECUTION_LEDGER_START_FAILED",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                **trace_fields,
                "release": released,
                "safe_to_retry": False,
            }

        options = dict(adapter_options or {})
        options["provider_id"] = provider_id
        options["request_id"] = request_id
        options["idempotency_key"] = request_id
        options["trace_id"] = trace_context.trace_id
        options["traceparent"] = trace_context.traceparent
        options["trace_headers"] = trace_context.headers
        started_at = time.monotonic()
        try:
            adapter_result = adapter(**options)
        except Exception as exc:
            # The adapter may have transmitted the request before failing. Do not
            # release reservations, and persist an ambiguous terminal state to block replay.
            self._ledger_finish(
                request_id=request_id, status="UNKNOWN",
                result_status="EXECUTION_OUTCOME_UNKNOWN", provider_id=provider_id,
            )
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                **trace_fields,
                "reservations_retained": True,
                "safe_to_retry": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(adapter_result, dict) or not isinstance(adapter_result.get("started"), bool):
            self._ledger_finish(
                request_id=request_id, status="UNKNOWN",
                result_status="EXECUTION_OUTCOME_UNKNOWN", provider_id=provider_id,
            )
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                **trace_fields,
                "reservations_retained": True,
                "safe_to_retry": False,
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
                self._ledger_finish(
                    request_id=request_id, status="UNKNOWN",
                    result_status="INCONSISTENT_START_RESULT", provider_id=provider_id,
                )
                return {
                    "status": "INCONSISTENT_START_RESULT",
                    "executed": False,
                    "provider": provider_id,
                    "request_id": request_id,
                    **trace_fields,
                    "reservations_retained": True,
                    "safe_to_retry": False,
                }
            released = self.release_unstarted_request(
                project_id=project_id, provider_id=provider_id, request_id=request_id
            )
            unstarted_status = "EXECUTION_NOT_STARTED" if released.get("released") is True else "RESERVATION_RELEASE_FAILED"
            finished = self._ledger_finish(
                request_id=request_id, status="NOT_STARTED",
                result_status=unstarted_status, provider_id=provider_id,
            )
            if not isinstance(finished, dict) or finished.get("saved") is not True:
                unstarted_status = "EXECUTION_LEDGER_FINISH_FAILED"
            return {
                "status": unstarted_status,
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                **trace_fields,
                "release": released,
                "safe_to_retry": False,
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
        final_success = reported_success
        output_error = None
        if reported_success:
            final_path = adapter_result.get("output_path", output_path)
            if final_path is None:
                output_validation = {"valid": False, "status": "OUTPUT_PATH_REQUIRED"}
            else:
                output_validation = self.validate_output(final_path, modality=modality)
            if not isinstance(output_validation, dict) or output_validation.get("valid") is not True:
                final_success = False
                output_error = "OUTPUT_VALIDATION_FAILED"

        error_code = self._safe_error_code(
            adapter_result.get("error_code"),
            output_error or ("PROVIDER_EXECUTION_FAILED" if not final_success else "PROVIDER_EXECUTION_SUCCEEDED"),
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
            trace_id=trace_context.trace_id,
        )
        retry = None
        if not final_success and self.retry_policy is not None:
            retry = self.retry_decision(
                attempt=adapter_result.get("attempt", 1),
                status_code=adapter_result.get("status_code"),
                retry_after=adapter_result.get("retry_after"),
                idempotent=adapter_result.get("idempotent", False),
            )
        if accounting_required:
            status = "EXECUTION_ACCOUNTING_REQUIRED"
        elif output_error is not None:
            status = "OUTPUT_VALIDATION_FAILED"
        elif not final_success:
            status = "EXECUTION_FAILED"
        elif finalization.get("finalized") is True:
            status = "EXECUTION_SUCCEEDED"
        else:
            status = "EXECUTION_FINALIZATION_PARTIAL"
        ledger_state = (
            "COMPLETED" if status == "EXECUTION_SUCCEEDED"
            else "FAILED" if status in {"OUTPUT_VALIDATION_FAILED", "EXECUTION_FAILED"}
            else "FINALIZATION_PARTIAL"
        )
        ledger_finish = self._ledger_finish(
            request_id=request_id,
            status=ledger_state,
            result_status=status,
            provider_id=provider_id,
        )
        if not isinstance(ledger_finish, dict) or ledger_finish.get("saved") is not True:
            return {
                "status": "EXECUTION_LEDGER_FINALIZATION_FAILED",
                "original_status": status,
                "executed": True,
                "provider": provider_id,
                "request_id": request_id,
                **trace_fields,
                "billing_mode": route.get("billing_mode"),
                "output": output_validation,
                "finalization": finalization,
                "retry_decision": retry,
                "safe_to_retry": False,
            }
        return {
            "status": status,
            "executed": True,
            "provider": provider_id,
            "request_id": request_id,
            **trace_fields,
            "billing_mode": route.get("billing_mode"),
            "output": output_validation,
            "finalization": finalization,
            "retry_decision": retry,
            "idempotency_status": "RECORDED",
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
        trace_id: str | None = None,
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
        if trace_id is not None and (
            not isinstance(trace_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
            or not any(char != "0" for char in trace_id)
        ):
            return {"status": "INVALID_TRACE_ID", "finalized": False}
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
                    trace_id=trace_id,
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
