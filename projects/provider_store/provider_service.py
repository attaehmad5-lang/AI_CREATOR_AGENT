"""Integration facade for provider connection, routing, health, cost, quota, and outputs."""
from __future__ import annotations

from typing import Any
import inspect
import re
import time

from .safe_numbers import finite_number
from .provider_error_policy import classify_provider_error
from .secrets_manager import SecretManagerError


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
        identity_authenticator: Any | None = None,
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
        project_policy_manager: Any | None = None,
        slo_manager: Any | None = None,
        degraded_mode_manager: Any | None = None,
        tenant_isolation_manager: Any | None = None,
        secret_manager: Any | None = None,
    ) -> None:
        if not callable(getattr(connection_manager, "verify_connectivity", None)):
            raise ValueError("connection_manager must support connectivity verification")
        if tenant_isolation_manager is not None and authorization_manager is None:
            raise ValueError("tenant isolation requires authorization_manager")
        if identity_authenticator is not None and authorization_manager is None:
            raise ValueError("identity authentication requires authorization_manager")
        if identity_authenticator is not None and (
            not callable(getattr(identity_authenticator, "authenticate", None))
            or not callable(getattr(identity_authenticator, "is_authenticated_principal", None))
        ):
            raise ValueError("identity_authenticator must verify tokens and identify trusted principals")
        resolved_secret_manager = secret_manager or getattr(connection_manager, "secret_manager", None)
        if resolved_secret_manager is not None and (
            not callable(getattr(resolved_secret_manager, "resolve_secret", None))
            or not callable(getattr(resolved_secret_manager, "rotate_secret", None))
        ):
            raise ValueError("secret_manager must support secret resolution and rotation")
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
            (project_policy_manager, "simulate_policy", "project_policy_manager"),
            (slo_manager, "record", "slo_manager"),
            (slo_manager, "configure", "slo_manager"),
            (slo_manager, "evaluate", "slo_manager"),
            (degraded_mode_manager, "evaluate", "degraded_mode_manager"),
            (degraded_mode_manager, "get_state", "degraded_mode_manager"),
            (tenant_isolation_manager, "authorize_project", "tenant_isolation_manager"),
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
        self.identity_authenticator = identity_authenticator
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
        self.project_policy_manager = project_policy_manager
        self.slo_manager = slo_manager
        self.degraded_mode_manager = degraded_mode_manager
        self.tenant_isolation_manager = tenant_isolation_manager
        self.secret_manager = resolved_secret_manager

    def authenticate_bearer_token(self, authorization_header: str) -> dict[str, Any]:
        """Verify an API bearer token and return an opaque in-process principal.

        API middleware must keep principal_id server-side, never accept it from
        request JSON, and pass this exact object to protected service methods.
        Do not cast it to str or serialize it into a client response.
        """
        if self.identity_authenticator is None:
            return {"status": "IDENTITY_AUTHENTICATOR_UNAVAILABLE", "authenticated": False}
        if not isinstance(authorization_header, str) or len(authorization_header) > 65_543:
            return {"status": "OIDC_TOKEN_INVALID", "authenticated": False}
        match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", authorization_header)
        if match is None:
            return {"status": "OIDC_TOKEN_INVALID", "authenticated": False}
        try:
            principal = self.identity_authenticator.authenticate(match.group(1))
            trusted = self.identity_authenticator.is_authenticated_principal(principal)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", status):
                status = "OIDC_AUTHENTICATION_FAILED"
            return {"status": status, "authenticated": False}
        if not trusted or not isinstance(principal, str):
            return {"status": "OIDC_AUTHENTICATION_FAILED", "authenticated": False}
        return {"status": "AUTHENTICATED", "authenticated": True, "principal_id": principal}

    def _authorize_operation(
        self,
        principal_id: str | None,
        operation: str,
        *,
        provider_id: str | None = None,
        return_decision: bool = False,
    ) -> dict[str, Any] | None:
        if self.identity_authenticator is not None:
            try:
                trusted_identity = self.identity_authenticator.is_authenticated_principal(principal_id)
            except Exception:
                trusted_identity = False
            if trusted_identity is not True:
                return {"allowed": False, "status": "OIDC_AUTHENTICATION_REQUIRED"}
            # Convert only after the in-process capability check succeeds.
            principal_id = str(principal_id)
        if self.authorization_manager is None:
            if principal_id is not None:
                return {"allowed": False, "status": "AUTHORIZATION_MANAGER_UNAVAILABLE"}
            # principal_id=None is reserved for trusted in-process calls only.
            return {"allowed": True, "status": "TRUSTED_INTERNAL"} if return_decision else None
        try:
            authorize = self.authorization_manager.authorize
            if provider_id is not None:
                if getattr(self.authorization_manager, "supports_provider_scopes", False) is not True:
                    return {
                        "allowed": False,
                        "status": "PROVIDER_SCOPE_ENFORCEMENT_UNAVAILABLE",
                    }
                result = authorize(principal_id, operation, provider_id=provider_id)
            else:
                result = authorize(principal_id, operation)
        except Exception as exc:
            return {"allowed": False, "status": "AUTHORIZATION_CHECK_FAILED", "error_type": type(exc).__name__}
        if not isinstance(result, dict):
            result = {"allowed": False, "status": "INVALID_AUTHORIZATION_RESPONSE"}
        if result.get("allowed") is not True:
            return result
        return result if return_decision else None


    def set_provider_access_scopes(
        self,
        target_principal_id: str,
        provider_scopes: dict[str, list[str]] | None,
        *,
        actor_principal_id: str | None,
    ) -> dict[str, Any]:
        """Set operation-specific provider allowlists under admin authorization."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(actor_principal_id, "provider.access.manage")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        setter = getattr(self.authorization_manager, "set_provider_scopes", None)
        if not callable(setter):
            return {"status": "AUTHORIZATION_SCOPE_MANAGER_UNAVAILABLE", "saved": False}
        try:
            result = setter(target_principal_id, provider_scopes)
        except Exception as exc:
            return {"status": "PROVIDER_SCOPE_UPDATE_FAILED", "saved": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {
            "status": "INVALID_AUTHORIZATION_MANAGER_RESPONSE", "saved": False
        }

    def configure_provider(
        self,
        provider_id: str,
        *,
        principal_id: str | None,
        model: str | None = None,
        endpoint: str | None = None,
        probe_endpoint: str | None = None,
        authentication_required: bool | None = None,
    ) -> dict[str, Any]:
        """Authorize provider-scoped configuration before updating connection state."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "provider": provider_id, "saved": False}
        authorization = self._authorize_operation(
            principal_id, "provider.configure", provider_id=provider_id
        )
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "provider": provider_id, "saved": False, "authorization": authorization}
        configure = getattr(self.connection_manager, "configure_provider", None)
        if not callable(configure):
            return {"status": "CONNECTION_MANAGER_UNAVAILABLE", "provider": provider_id, "saved": False}
        try:
            return configure(
                provider_id, model=model, endpoint=endpoint,
                probe_endpoint=probe_endpoint,
                authentication_required=authentication_required,
            )
        except Exception as exc:
            return {"status": "PROVIDER_CONFIGURATION_FAILED", "provider": provider_id, "saved": False, "error_type": type(exc).__name__}

    def set_provider_enabled(
        self,
        provider_id: str,
        enabled: bool,
        *,
        principal_id: str | None,
    ) -> dict[str, Any]:
        """Enable or disable a provider only with the matching admin operation."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "provider": provider_id, "saved": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_VALUE", "provider": provider_id, "saved": False}
        operation = "provider.enable" if enabled else "provider.disable"
        authorization = self._authorize_operation(
            principal_id, operation, provider_id=provider_id
        )
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "provider": provider_id, "saved": False, "authorization": authorization}
        setter = getattr(self.connection_manager, "set_provider_enabled", None)
        if not callable(setter):
            return {"status": "CONNECTION_MANAGER_UNAVAILABLE", "provider": provider_id, "saved": False}
        try:
            return setter(provider_id, enabled)
        except Exception as exc:
            return {"status": "PROVIDER_ENABLEMENT_FAILED", "provider": provider_id, "saved": False, "error_type": type(exc).__name__}

    def assign_provider_role(
        self,
        target_principal_id: str,
        role: str,
        *,
        actor_principal_id: str | None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Assign a role only after an authenticated administrator is authorized."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(
            actor_principal_id, "provider.access.manage"
        )
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        assign = getattr(self.authorization_manager, "assign_role", None)
        if not callable(assign):
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        try:
            result = assign(target_principal_id, role, enabled=enabled)
        except Exception as exc:
            return {"status": "ROLE_ASSIGNMENT_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict):
            return {"status": "INVALID_AUTHORIZATION_MANAGER_RESPONSE", "saved": False}
        return result

    def disable_provider_principal(
        self,
        target_principal_id: str,
        *,
        actor_principal_id: str | None,
    ) -> dict[str, Any]:
        """Disable an identity only through the administrator-gated service facade."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(
            actor_principal_id, "provider.access.manage"
        )
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        disable = getattr(self.authorization_manager, "disable_principal", None)
        if not callable(disable):
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        try:
            result = disable(target_principal_id)
        except Exception as exc:
            return {"status": "PRINCIPAL_DISABLE_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict):
            return {"status": "INVALID_AUTHORIZATION_MANAGER_RESPONSE", "saved": False}
        return result

    def _authorize_tenant_project(
        self,
        *,
        tenant_id: str | None,
        project_id: str,
        principal_id: str | None,
    ) -> dict[str, Any] | None:
        """Require a matching tenant/project/principal binding when tenant mode is active."""
        if self.tenant_isolation_manager is None:
            return None
        if (
            not isinstance(tenant_id, str) or not tenant_id.strip()
            or len(tenant_id) > 128
        ):
            return {"status": "TENANT_CONTEXT_REQUIRED"}
        try:
            result = self.tenant_isolation_manager.authorize_project(
                tenant_id, project_id, principal_id
            )
        except Exception as exc:
            return {
                "status": "TENANT_ISOLATION_CHECK_FAILED",
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or result.get("allowed") is not True:
            return {"status": "TENANT_ISOLATION_DENIED"}
        return None

    def configure_category_degraded_mode(
        self,
        category: str,
        *,
        normal_min_healthy_providers: int = 2,
        degraded_min_healthy_providers: int = 1,
        recovery_success_samples: int = 3,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Configure outage/degraded thresholds using admin-only permission."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(principal_id, "provider.configure")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        if self.degraded_mode_manager is None:
            return {"status": "DEGRADED_MODE_MANAGER_UNAVAILABLE", "saved": False}
        try:
            result = self.degraded_mode_manager.configure(
                category,
                normal_min_healthy_providers=normal_min_healthy_providers,
                degraded_min_healthy_providers=degraded_min_healthy_providers,
                recovery_success_samples=recovery_success_samples,
            )
        except Exception as exc:
            return {"status": "DEGRADED_MODE_CONFIGURATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict) or result.get("saved") is not True:
            return result if isinstance(result, dict) else {
                "status": "INVALID_DEGRADED_MODE_CONFIGURATION_RESPONSE", "saved": False,
            }
        return result

    def evaluate_category_degraded_mode(
        self,
        category: str,
        provider_ids: list[str],
        *,
        stale_after_seconds: float = 300.0,
        max_latency_ms: float | None = None,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate configured mode from current health evidence, fail-closed on missing probes."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "evaluated": False}
        authorization = self._authorize_operation(principal_id, "provider.verify")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "evaluated": False, "authorization": authorization}
        if self.degraded_mode_manager is None:
            return {"status": "DEGRADED_MODE_MANAGER_UNAVAILABLE", "evaluated": False}
        is_eligible = getattr(self.health_manager, "is_eligible", None)
        if not callable(is_eligible):
            return {"status": "HEALTH_MANAGER_UNAVAILABLE", "evaluated": False}
        if not isinstance(provider_ids, list) or not 1 <= len(provider_ids) <= 1000:
            return {"status": "INVALID_PROVIDER_LIST", "evaluated": False}
        statuses: list[dict[str, Any]] = []
        seen: set[str] = set()
        for provider_id in provider_ids:
            if not isinstance(provider_id, str) or not provider_id.strip() or provider_id in seen:
                return {"status": "INVALID_PROVIDER_LIST", "evaluated": False}
            seen.add(provider_id)
            try:
                evidence = is_eligible(
                    provider_id,
                    stale_after_seconds=stale_after_seconds,
                    max_latency_ms=max_latency_ms,
                )
            except Exception as exc:
                evidence = {"eligible": False, "status": "HEALTH_PROBE_FAILED", "error_type": type(exc).__name__}
            eligible = isinstance(evidence, dict) and evidence.get("eligible") is True
            statuses.append({"provider_id": provider_id, "eligible": eligible})
        try:
            result = self.degraded_mode_manager.evaluate(category, statuses)
        except Exception as exc:
            return {"status": "DEGRADED_MODE_EVALUATION_FAILED", "evaluated": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict) or result.get("evaluated") is not True:
            return result if isinstance(result, dict) else {
                "status": "INVALID_DEGRADED_MODE_RESPONSE", "evaluated": False,
            }
        return result

    def configure_service_level_objective(
        self,
        slo_id: str,
        *,
        provider_id: str,
        category: str,
        availability_target: float,
        max_p95_latency_ms: float,
        window_seconds: int = 3600,
        minimum_samples: int = 20,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Configure an SLO using administrator-only provider configuration permission."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(principal_id, "provider.configure", provider_id=provider_id)
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        if self.slo_manager is None:
            return {"status": "SLO_MANAGER_UNAVAILABLE", "saved": False}
        try:
            result = self.slo_manager.configure(
                slo_id, provider_id=provider_id, category=category,
                availability_target=availability_target,
                max_p95_latency_ms=max_p95_latency_ms,
                window_seconds=window_seconds,
                minimum_samples=minimum_samples,
            )
        except Exception as exc:
            return {"status": "SLO_CONFIGURATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict) or result.get("saved") is not True:
            return result if isinstance(result, dict) else {
                "status": "INVALID_SLO_CONFIGURATION_RESPONSE", "saved": False,
            }
        return result

    def evaluate_service_level_objective(
        self,
        slo_id: str,
        *,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Read a rolling SLO evaluation after provider.read authorization."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_UNAVAILABLE", "evaluation": None}
        authorization = self._authorize_operation(principal_id, "provider.read")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "evaluation": None, "authorization": authorization}
        if self.slo_manager is None:
            return {"status": "SLO_MANAGER_UNAVAILABLE", "evaluation": None}
        try:
            result = self.slo_manager.evaluate(slo_id)
        except Exception as exc:
            return {"status": "SLO_EVALUATION_FAILED", "evaluation": None, "error_type": type(exc).__name__}
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            return {"status": "INVALID_SLO_EVALUATION_RESPONSE", "evaluation": None}
        return result

    def simulate_project_provider_policy(
        self,
        project_id: str,
        candidate_policy: dict[str, Any],
        scenarios: list[dict[str, Any]],
        *,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Run an admin-authorized policy preview without changing active policy."""
        if self.authorization_manager is None:
            return {
                "status": "AUTHORIZATION_MANAGER_UNAVAILABLE",
                "project_id": project_id,
                "persisted": False,
            }
        authorization = self._authorize_operation(principal_id, "provider.policy.simulate")
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED",
                "project_id": project_id,
                "persisted": False,
                "authorization": authorization,
            }
        if self.project_policy_manager is None:
            return {
                "status": "PROJECT_POLICY_MANAGER_UNAVAILABLE",
                "project_id": project_id,
                "persisted": False,
            }
        try:
            result = self.project_policy_manager.simulate_policy(
                project_id, candidate_policy, scenarios
            )
        except Exception as exc:
            return {
                "status": "POLICY_SIMULATION_FAILED",
                "project_id": project_id,
                "persisted": False,
                "error_type": type(exc).__name__,
            }
        valid_statuses = {
            "POLICY_SIMULATION_COMPLETED",
            "INVALID_CANDIDATE_POLICY",
            "INVALID_SIMULATION_SCENARIOS",
        }
        if (
            not isinstance(result, dict)
            or result.get("status") not in valid_statuses
            or result.get("persisted") is not False
        ):
            return {
                "status": "INVALID_POLICY_SIMULATION_RESPONSE",
                "project_id": project_id,
                "persisted": False,
            }
        return result

    def active_probe_provider(
        self,
        provider_id: str,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        """Run one bounded, authorized provider probe and return only safe evidence."""
        authorization = self._authorize_operation(principal_id, "provider.verify", provider_id=provider_id)
        if authorization is not None:
            return {
                "status": "AUTHORIZATION_DENIED", "provider": provider_id,
                "verified": False, "saved": False, "authorization": authorization,
            }
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "provider": None, "verified": False, "saved": False}
        timeout = finite_number(timeout_seconds, minimum=0)
        if timeout is None or timeout <= 0 or timeout > 30:
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "verified": False, "saved": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "provider": provider_id, "verified": False, "saved": False}

        get_provider = getattr(self.connection_manager, "get_provider", None)
        if not callable(get_provider):
            return {"status": "PROVIDER_LOOKUP_UNAVAILABLE", "provider": provider_id, "verified": False, "saved": False}
        try:
            provider = get_provider(provider_id)
        except Exception as exc:
            return {
                "status": "PROVIDER_LOOKUP_FAILED", "provider": provider_id,
                "verified": False, "saved": False, "error_type": type(exc).__name__,
            }
        if not isinstance(provider, dict):
            return {"status": "NOT_FOUND", "provider": provider_id, "verified": False, "saved": False}
        if provider.get("enabled") is not True:
            return {"status": "PROVIDER_DISABLED", "provider": provider_id, "verified": False, "saved": False}

        if self.health_manager is None:
            return {"status": "HEALTH_MANAGER_UNAVAILABLE", "provider": provider_id, "verified": False, "saved": False}
        active_probe = getattr(self.health_manager, "active_probe", None)
        if not callable(active_probe):
            return {"status": "ACTIVE_PROBE_UNAVAILABLE", "provider": provider_id, "verified": False, "saved": False}
        try:
            result = active_probe(provider_id, probe, timeout_seconds=timeout)
        except Exception as exc:
            return {
                "status": "ACTIVE_PROBE_FAILED", "provider": provider_id,
                "verified": False, "saved": False, "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict):
            return {"status": "INVALID_ACTIVE_PROBE_RESPONSE", "provider": provider_id, "verified": False, "saved": False}

        status = result.get("status")
        recorded_statuses = {"HEALTHY", "TIMEOUT", "NETWORK_ERROR", "PROBE_FAILED"}
        if not isinstance(status, str) or status not in recorded_statuses | {"HEALTH_RECORD_FAILED"}:
            return {"status": "INVALID_ACTIVE_PROBE_RESPONSE", "provider": provider_id, "verified": False, "saved": False}
        if (status == "HEALTHY") != (result.get("verified") is True):
            return {"status": "INVALID_ACTIVE_PROBE_RESPONSE", "provider": provider_id, "verified": False, "saved": False}
        verified = status == "HEALTHY" and result.get("verified") is True
        saved = status in recorded_statuses
        response: dict[str, Any] = {
            "status": status,
            "provider": provider_id,
            "verified": verified,
            "saved": saved,
        }
        if verified:
            latency = finite_number(result.get("latency_ms"), minimum=0)
            if latency is not None:
                response["latency_ms"] = round(latency, 2)
        error_type = result.get("error_type")
        if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
            response["error_type"] = error_type
        return response

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
        authorization = self._authorize_operation(principal_id, "provider.verify", provider_id=provider_id)
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

    def rotate_provider_secret(
        self,
        provider_id: str,
        *,
        idempotency_key: str,
        confirmed: bool,
        principal_id: str | None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Rotate a provider secret through its configured vault after admin approval."""
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_REQUIRED", "rotated": False}
        authorization = self._authorize_operation(
            principal_id, "provider.credentials.rotate", provider_id=provider_id
        )
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "rotated": False}
        if self.secret_manager is None:
            return {"status": "SECRET_MANAGER_UNAVAILABLE", "rotated": False}
        if self.tenant_isolation_manager is not None and (
            getattr(self.secret_manager, "tenant_isolation_manager", None)
            is not self.tenant_isolation_manager
        ):
            return {"status": "TENANT_CREDENTIAL_AUTHORIZATION_NOT_CONFIGURED", "rotated": False}
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER_ID", "rotated": False}
        try:
            provider = self.connection_manager.get_provider(provider_id)
        except Exception as exc:
            return {"status": "PROVIDER_LOOKUP_FAILED", "rotated": False, "error_type": type(exc).__name__}
        if not isinstance(provider, dict):
            return {"status": "PROVIDER_NOT_FOUND", "rotated": False}
        secret_ref = provider.get("secret_ref")
        if not isinstance(secret_ref, str):
            return {"status": "PROVIDER_SECRET_REFERENCE_REQUIRED", "rotated": False}
        try:
            result = self.secret_manager.rotate_secret(
                secret_ref,
                provider_id=provider_id,
                idempotency_key=idempotency_key,
                principal_id=principal_id,
                confirmed=confirmed,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            return {
                "status": "SECRET_ROTATION_FAILED",
                "rotated": False,
                "error_type": type(exc).__name__,
            }
        if (
            not isinstance(result, dict)
            or result.get("rotated") is not True
            or result.get("status") != "SECRET_ROTATED"
        ):
            candidate_status = result.get("status") if isinstance(result, dict) else None
            safe_status = (
                candidate_status
                if isinstance(candidate_status, str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", candidate_status)
                else "INVALID_ROTATION_RESPONSE"
            )
            return {"status": safe_status, "rotated": False}
        return {
            "status": "PROVIDER_SECRET_ROTATED",
            "rotated": True,
            "provider_id": provider_id,
            "backend": result.get("backend"),
            **({"version_id": result["version_id"]} if isinstance(result.get("version_id"), str) else {}),
            **({"rotated_at": result["rotated_at"]} if isinstance(result.get("rotated_at"), str) else {}),
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
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a configured allowlisted HTTPS endpoint using the built-in safe probe.

        This checks reachability and authentication only; it does not certify model
        support, account credits, generation quality, or provider-specific capabilities.
        """
        authorization = self._authorize_operation(principal_id, "provider.verify", provider_id=provider_id)
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
                secret_manager=self.secret_manager,
                tenant_id=tenant_id,
                principal_id=principal_id,
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
        authorization = self._authorize_operation(principal_id, "provider.verify", provider_id=provider_id)
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
        authorization = self._authorize_operation(principal_id, "provider.verify", provider_id=provider_id)
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

    @staticmethod
    def _sanitize_job_response(result: dict[str, Any], sensitive_data_policy: Any | None = None) -> dict[str, Any]:
        """Redact worker-only lease tokens and fail closed on sensitive stored job data.

        Claim responses are intentionally excluded: only the worker claiming a job
        receives its lease token. Status, completion, submission and cancellation
        responses must never disclose that capability to ordinary API callers.
        """
        safe_result = dict(result)
        job = safe_result.get("job")
        if not isinstance(job, dict):
            return safe_result

        safe_job = dict(job)
        safe_job.pop("lease_token", None)
        safe_job.pop("lease_token_hash", None)
        if sensitive_data_policy is not None:
            try:
                scan = sensitive_data_policy.inspect(safe_job)
            except Exception:
                scan = {
                    "allowed": False,
                    "status": "SENSITIVE_DATA_SCAN_FAILED",
                    "findings": [],
                    "blocked_categories": [],
                }
            if not isinstance(scan, dict) or scan.get("allowed") is not True:
                return {
                    "status": "SENSITIVE_DATA_BLOCKED",
                    "job": None,
                    "sensitive_data_scan": scan if isinstance(scan, dict) else {
                        "allowed": False,
                        "status": "INVALID_SENSITIVE_DATA_SCAN_RESPONSE",
                        "findings": [],
                        "blocked_categories": [],
                    },
                }
        safe_result["job"] = safe_job
        return safe_result

    def _sensitive_input_rejection(self, payload: Any) -> dict[str, Any] | None:
        """Return a safe rejection before untrusted metadata reaches a router or store."""
        if self.sensitive_data_policy is None:
            return None
        try:
            scan = self.sensitive_data_policy.inspect(payload)
        except Exception as exc:
            return {
                "status": "SENSITIVE_DATA_SCAN_FAILED",
                "error_type": type(exc).__name__,
            }
        if not isinstance(scan, dict) or scan.get("allowed") is not True:
            return {
                "status": "SENSITIVE_DATA_BLOCKED",
                "sensitive_data_scan": scan if isinstance(scan, dict) else {
                    "allowed": False,
                    "status": "INVALID_SENSITIVE_DATA_SCAN_RESPONSE",
                    "findings": [],
                    "blocked_categories": [],
                },
            }
        return None

    def submit_job(
        self, *, job_id: str, project_id: str, provider_id: str,
        payload_ref: str, priority: int = 50, principal_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "accepted": False}
        authorization = self._authorize_operation(principal_id, "provider.generate", provider_id=provider_id)
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "accepted": False, "authorization": authorization}
        tenant_error = self._authorize_tenant_project(
            tenant_id=tenant_id, project_id=project_id, principal_id=principal_id
        )
        if tenant_error is not None:
            return {"status": tenant_error["status"], "accepted": False}
        rejection = self._sensitive_input_rejection({
            "job_submission": {
                "job_id": job_id,
                "project_id": project_id,
                "provider_id": provider_id,
                "payload_ref": payload_ref,
                "priority": priority,
                "tenant_id": tenant_id,
            },
        })
        if rejection is not None:
            return {"accepted": False, **rejection}
        try:
            submit_args = {
                "job_id": job_id, "project_id": project_id, "provider_id": provider_id,
                "payload_ref": payload_ref, "priority": priority,
            }
            if self.tenant_isolation_manager is not None:
                submit_args["tenant_id"] = tenant_id
            result = self.job_manager.submit(**submit_args)
        except Exception as exc:
            return {"status": "JOB_SUBMISSION_FAILED", "accepted": False, "error_type": type(exc).__name__}
        return self._sanitize_job_response(result, self.sensitive_data_policy) if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "accepted": False}

    def poll_job(
        self, *, job_id: str, principal_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "job": None}
        authorization = self._authorize_operation(
            principal_id, "provider.read", provider_id="*", return_decision=True
        )
        if not isinstance(authorization, dict) or authorization.get("allowed") is not True:
            return {"status": "AUTHORIZATION_DENIED", "job": None, "authorization": authorization}
        if self.tenant_isolation_manager is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            return {"status": "TENANT_CONTEXT_REQUIRED", "job": None}
        try:
            result = (
                self.job_manager.poll(job_id=job_id, tenant_id=tenant_id)
                if self.tenant_isolation_manager is not None
                else self.job_manager.poll(job_id=job_id)
            )
        except Exception as exc:
            return {"status": "JOB_POLL_FAILED", "job": None, "error_type": type(exc).__name__}
        if not isinstance(result, dict):
            return {"status": "INVALID_JOB_MANAGER_RESPONSE", "job": None}
        job = result.get("job")
        if isinstance(job, dict):
            scoped_authorization = self._authorize_operation(
                principal_id, "provider.read", provider_id=job.get("provider_id")
            )
            if scoped_authorization is not None:
                return {"status": "AUTHORIZATION_DENIED", "job": None, "authorization": scoped_authorization}
        if self.tenant_isolation_manager is not None and isinstance(job, dict):
            tenant_error = self._authorize_tenant_project(
                tenant_id=tenant_id,
                project_id=job.get("project_id"),
                principal_id=principal_id,
            )
            if tenant_error is not None:
                return {"status": tenant_error["status"], "job": None}
        return self._sanitize_job_response(result, self.sensitive_data_policy)

    def claim_next_job(self, *, worker_id: str, principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "job": None}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "job": None, "authorization": authorization}
        rejection = self._sensitive_input_rejection({"worker_id": worker_id})
        if rejection is not None:
            return {"job": None, **rejection}
        try:
            result = self.job_manager.claim_next(worker_id=worker_id)
        except Exception as exc:
            return {"status": "JOB_CLAIM_FAILED", "job": None, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "job": None}

    def renew_job_lease(
        self, *, job_id: str, worker_id: str, lease_token: str,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "renewed": False}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "renewed": False, "authorization": authorization}
        try:
            result = self.job_manager.renew_lease(
                job_id=job_id, worker_id=worker_id, lease_token=lease_token
            )
        except Exception as exc:
            return {"status": "JOB_LEASE_RENEWAL_FAILED", "renewed": False, "error_type": type(exc).__name__}
        return result if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "renewed": False}

    def complete_job(self, *, job_id: str, worker_id: str, lease_token: str, success: bool,
                     result_ref: str | None = None, error_code: str | None = None,
                     principal_id: str | None = None) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(principal_id, "provider.generate")
        if authorization is not None:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        rejection = self._sensitive_input_rejection({
            "job_completion": {
                "job_id": job_id,
                "worker_id": worker_id,
                "success": success,
                "result_ref": result_ref,
                "error_code": error_code,
            },
        })
        if rejection is not None:
            return {"saved": False, **rejection}
        try:
            result = self.job_manager.complete(
                job_id=job_id, worker_id=worker_id, lease_token=lease_token, success=success,
                result_ref=result_ref, error_code=error_code,
            )
        except Exception as exc:
            return {"status": "JOB_COMPLETION_FAILED", "saved": False, "error_type": type(exc).__name__}
        return self._sanitize_job_response(result, self.sensitive_data_policy) if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "saved": False}

    def cancel_job(
        self, *, job_id: str, principal_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if self.job_manager is None:
            return {"status": "JOB_MANAGER_UNAVAILABLE", "saved": False}
        authorization = self._authorize_operation(
            principal_id, "provider.generate", provider_id="*", return_decision=True
        )
        if not isinstance(authorization, dict) or authorization.get("allowed") is not True:
            return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": authorization}
        if self.tenant_isolation_manager is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            return {"status": "TENANT_CONTEXT_REQUIRED", "saved": False}
        try:
            existing = (
                self.job_manager.poll(job_id=job_id, tenant_id=tenant_id)
                if self.tenant_isolation_manager is not None
                else self.job_manager.poll(job_id=job_id)
            )
            if not isinstance(existing, dict) or not isinstance(existing.get("job"), dict):
                return {
                    "status": existing.get("status", "JOB_NOT_FOUND") if isinstance(existing, dict) else "INVALID_JOB_MANAGER_RESPONSE",
                    "saved": False,
                }
            job = existing["job"]
            scoped_authorization = self._authorize_operation(
                principal_id, "provider.generate", provider_id=job.get("provider_id")
            )
            if scoped_authorization is not None:
                return {"status": "AUTHORIZATION_DENIED", "saved": False, "authorization": scoped_authorization}
            if self.tenant_isolation_manager is not None:
                tenant_error = self._authorize_tenant_project(
                    tenant_id=tenant_id,
                    project_id=job.get("project_id"),
                    principal_id=principal_id,
                )
                if tenant_error is not None:
                    return {"status": tenant_error["status"], "saved": False}
                result = self.job_manager.cancel(job_id=job_id, tenant_id=tenant_id)
            else:
                result = self.job_manager.cancel(job_id=job_id)
        except Exception as exc:
            return {"status": "JOB_CANCELLATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        return self._sanitize_job_response(result, self.sensitive_data_policy) if isinstance(result, dict) else {"status": "INVALID_JOB_MANAGER_RESPONSE", "saved": False}

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
        rejection = self._sensitive_input_rejection({"route_options": route_options})
        if rejection is not None:
            return {"allowed": False, **rejection}
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
        tenant_id: str | None = None,
        data_categories: tuple[str, ...] | list[str] = ("user_content",),
        retention_days: int = 0,
        provider_training_use: bool = False,
        user_training_consent: bool = False,
        output_path: Any = None,
        estimated_cost_unit: str = "USD",
        adapter_options: dict[str, Any] | None = None,
        require_modality_binding: bool = False,
        use_managed_credential: bool = False,
        require_fresh_health_check: bool = False,
        health_stale_after_seconds: float = 300.0,
        health_max_latency_ms: float | None = None,
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
        if not isinstance(use_managed_credential, bool):
            return {"status": "INVALID_MANAGED_CREDENTIAL_POLICY", "executed": False}
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        if not isinstance(category, str) or not category.strip():
            return {"status": "INVALID_CATEGORY", "executed": False}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"status": "INVALID_PROJECT_ID", "executed": False}
        if not self._valid_amount(estimated_cost):
            return {"status": "INVALID_COST_AMOUNT", "executed": False}

        degraded_mode_state = None
        if self.degraded_mode_manager is not None:
            try:
                degraded_state_result = self.degraded_mode_manager.get_state(category)
            except Exception as exc:
                return {
                    "status": "DEGRADED_MODE_STATE_FAILED",
                    "executed": False,
                    "error_type": type(exc).__name__,
                }
            degraded_mode_state = (
                degraded_state_result.get("state")
                if isinstance(degraded_state_result, dict) else None
            )
            if not isinstance(degraded_mode_state, dict):
                return {
                    "status": "PROVIDER_CATEGORY_HEALTH_UNKNOWN",
                    "executed": False,
                    "degraded_mode": degraded_state_result if isinstance(degraded_state_result, dict) else {
                        "status": "INVALID_DEGRADED_MODE_STATE_RESPONSE"
                    },
                }
            current_mode = degraded_mode_state.get("mode")
            healthy_provider_ids = degraded_mode_state.get("healthy_provider_ids")
            unhealthy_provider_ids = degraded_mode_state.get("unhealthy_provider_ids")
            valid_provider_ids = lambda values: (
                isinstance(values, list)
                and all(
                    isinstance(value, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}", value) is not None
                    for value in values
                )
                and len(set(values)) == len(values)
            )
            known_mode = isinstance(current_mode, str) and current_mode in {"NORMAL", "DEGRADED", "OUTAGE"}
            state_status = f"PROVIDER_CATEGORY_{current_mode}" if known_mode else None
            valid_degraded_state = (
                state_status is not None
                and degraded_mode_state.get("category") == category
                and degraded_state_result.get("status") == state_status
                and valid_provider_ids(healthy_provider_ids)
                and valid_provider_ids(unhealthy_provider_ids)
                and not (set(healthy_provider_ids) & set(unhealthy_provider_ids))
                and type(degraded_mode_state.get("healthy_count")) is int
                and degraded_mode_state["healthy_count"] == len(healthy_provider_ids)
                and type(degraded_mode_state.get("provider_count")) is int
                and degraded_mode_state["provider_count"] == len(healthy_provider_ids) + len(unhealthy_provider_ids)
                and (
                    bool(healthy_provider_ids)
                    if current_mode in {"NORMAL", "DEGRADED"}
                    else not healthy_provider_ids
                )
            )
            if not valid_degraded_state:
                return {
                    "status": "PROVIDER_CATEGORY_HEALTH_UNKNOWN",
                    "executed": False,
                    "degraded_mode": {"status": "INVALID_DEGRADED_MODE_STATE"},
                }
            if current_mode == "OUTAGE":
                return {
                    "status": "PROVIDER_CATEGORY_OUTAGE",
                    "executed": False,
                    "degraded_mode": degraded_state_result,
                    "preserve_started_request_accounting": True,
                }
            if current_mode == "DEGRADED":
                # Never route degraded-mode work without an up-to-date health gate.
                require_fresh_health_check = True

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

        authorization = self._authorize_operation(
            principal_id, "provider.generate", provider_id="*", return_decision=True
        )
        if not isinstance(authorization, dict) or authorization.get("allowed") is not True:
            return {
                "status": "AUTHORIZATION_DENIED", "executed": False,
                "authorization": authorization or {"allowed": False, "status": "INVALID_AUTHORIZATION_RESPONSE"},
            }
        allowed_provider_ids = authorization.get("allowed_provider_ids")
        if isinstance(allowed_provider_ids, list):
            route_options["allowed_provider_ids"] = set(allowed_provider_ids)

        if self.tenant_isolation_manager is not None:
            if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 128:
                return {"status": "TENANT_CONTEXT_REQUIRED", "executed": False}
            try:
                tenant_access = self.tenant_isolation_manager.authorize_project(
                    tenant_id, project_id, principal_id
                )
            except Exception as exc:
                return {
                    "status": "TENANT_ISOLATION_CHECK_FAILED",
                    "executed": False,
                    "error_type": type(exc).__name__,
                }
            if not isinstance(tenant_access, dict) or tenant_access.get("allowed") is not True:
                return {"status": "TENANT_ISOLATION_DENIED", "executed": False}

        sensitive_data_scan = None
        if self.sensitive_data_policy is not None:
            try:
                sensitive_data_scan = self.sensitive_data_policy.inspect({
                    "adapter_options": adapter_options or {},
                    "routing_options": route_options,
                })
            except Exception as exc:
                return {"status": "SENSITIVE_DATA_SCAN_FAILED", "executed": False, "error_type": type(exc).__name__}
            if not isinstance(sensitive_data_scan, dict) or sensitive_data_scan.get("allowed") is not True:
                return {
                    "status": "SENSITIVE_DATA_BLOCKED", "executed": False,
                    "sensitive_data_scan": sensitive_data_scan if isinstance(sensitive_data_scan, dict) else {
                        "allowed": False, "status": "INVALID_SENSITIVE_DATA_SCAN_RESPONSE"
                    },
                }

        if self.tenant_isolation_manager is not None:
            route_options["tenant_id"] = tenant_id
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
        scoped_authorization = self._authorize_operation(
            principal_id, "provider.generate", provider_id=provider_id
        )
        if scoped_authorization is not None:
            release = self.release_unstarted_request(
                project_id=project_id, provider_id=provider_id,
                request_id=request_id, tenant_id=tenant_id,
            )
            return {
                "status": "AUTHORIZATION_DENIED", "executed": False,
                "authorization": scoped_authorization,
                "reservation_release": release,
            }
        if isinstance(degraded_mode_state, dict) and degraded_mode_state.get("mode") == "DEGRADED":
            eligible_ids = degraded_mode_state.get("healthy_provider_ids")
            if not isinstance(eligible_ids, list) or provider_id not in eligible_ids:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id,
                    tenant_id=tenant_id,
                )
                return {
                    "status": "PROVIDER_NOT_ELIGIBLE_DURING_DEGRADED_MODE",
                    "executed": False,
                    "provider": provider_id,
                    "release": release,
                    "degraded_mode": degraded_mode_state,
                }

        if not isinstance(require_modality_binding, bool):
            release = self.release_unstarted_request(
                project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id
            )
            return {"status": "INVALID_MODALITY_BINDING_POLICY", "executed": False, "release": release}
        if require_modality_binding:
            binding_fn = getattr(self.connection_manager, "get_modality_binding", None)
            if not callable(binding_fn):
                binding = {"valid": False, "status": "MODALITY_BINDING_UNAVAILABLE"}
            else:
                try:
                    binding = binding_fn(provider_id)
                except Exception as exc:
                    binding = {"valid": False, "status": "MODALITY_BINDING_CHECK_FAILED", "error_type": type(exc).__name__}
            requested_modality = modality.strip().casefold() if isinstance(modality, str) else None
            allowed_modalities = binding.get("output_modalities") if isinstance(binding, dict) else None
            binding_valid = isinstance(binding, dict) and binding.get("valid") is True and isinstance(allowed_modalities, list)
            if not binding_valid or requested_modality not in allowed_modalities:
                release = self.release_unstarted_request(project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id)
                safe_binding = {
                    "valid": False,
                    "status": binding.get("status") if isinstance(binding, dict) and isinstance(binding.get("status"), str) else "INVALID_MODALITY_BINDING_RESPONSE",
                }
                if binding_valid:
                    safe_binding["category"] = binding.get("category")
                    safe_binding["output_modalities"] = list(allowed_modalities)
                    safe_binding["requested_modality"] = requested_modality[:64] if requested_modality is not None else None
                return {
                    "status": "MODALITY_MISMATCH" if binding_valid else "PROVIDER_MODALITY_REJECTED",
                    "executed": False, "provider": provider_id, "modality_binding": safe_binding, "release": release,
                }

        if not isinstance(require_fresh_health_check, bool):
            release = self.release_unstarted_request(project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id)
            return {"status": "INVALID_HEALTH_CHECK_POLICY", "executed": False, "release": release}
        if require_fresh_health_check:
            health_fn = getattr(self.health_manager, "is_eligible", None) if self.health_manager is not None else None
            if not callable(health_fn):
                health = {"eligible": False, "status": "HEALTH_CHECK_UNAVAILABLE"}
            else:
                try:
                    health = health_fn(
                        provider_id, stale_after_seconds=health_stale_after_seconds,
                        max_latency_ms=health_max_latency_ms,
                    )
                except Exception as exc:
                    health = {"eligible": False, "status": "HEALTH_CHECK_FAILED", "error_type": type(exc).__name__}
            if not isinstance(health, dict) or health.get("eligible") is not True:
                release = self.release_unstarted_request(project_id=project_id, provider_id=provider_id, request_id=request_id)
                safe_health = {
                    "eligible": False,
                    "status": health.get("status") if isinstance(health, dict) and isinstance(health.get("status"), str) else "INVALID_HEALTH_CHECK_RESPONSE",
                }
                if isinstance(health, dict) and isinstance(health.get("reason"), str):
                    safe_health["reason"] = health["reason"][:80]
                return {"status": "PROVIDER_HEALTH_REJECTED", "executed": False, "provider": provider_id, "health": safe_health, "release": release}

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
                    project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id
                )
                return {
                    "status": "PROVIDER_DATA_POLICY_CHECK_FAILED", "executed": False,
                    "release": release, "error_type": type(exc).__name__,
                }
            if not isinstance(data_policy_result, dict) or data_policy_result.get("allowed") is not True:
                release = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id
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
        secret_lease = None
        if use_managed_credential:
            try:
                adapter_parameters = inspect.signature(adapter).parameters.values()
                supports_managed_lease = any(
                    parameter.name == "provider_credential_lease"
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in adapter_parameters
                )
            except (TypeError, ValueError):
                supports_managed_lease = False
            if not supports_managed_lease:
                released = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id,
                    request_id=request_id, tenant_id=tenant_id,
                )
                return {
                    "status": "ADAPTER_MANAGED_CREDENTIAL_CONTRACT_UNSUPPORTED",
                    "executed": False,
                    "provider": provider_id,
                    "release": released,
                }
            if self.tenant_isolation_manager is not None and (
                getattr(self.secret_manager, "tenant_isolation_manager", None)
                is not self.tenant_isolation_manager
            ):
                released = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id,
                    request_id=request_id, tenant_id=tenant_id,
                )
                return {
                    "status": "TENANT_CREDENTIAL_AUTHORIZATION_NOT_CONFIGURED",
                    "executed": False,
                    "provider": provider_id,
                    "release": released,
                }
            if self.secret_manager is None:
                released = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id,
                    request_id=request_id, tenant_id=tenant_id,
                )
                return {
                    "status": "SECRET_MANAGER_UNAVAILABLE",
                    "executed": False,
                    "provider": provider_id,
                    "release": released,
                }
            if "provider_credential" in options or "provider_credential_lease" in options:
                released = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id,
                    request_id=request_id, tenant_id=tenant_id,
                )
                return {
                    "status": "CALLER_SUPPLIED_MANAGED_CREDENTIAL_REJECTED",
                    "executed": False,
                    "provider": provider_id,
                    "release": released,
                }
            try:
                provider_record = self.connection_manager.get_provider(provider_id)
                secret_ref = provider_record.get("secret_ref") if isinstance(provider_record, dict) else None
                if not isinstance(secret_ref, str):
                    raise SecretManagerError("PROVIDER_SECRET_REFERENCE_REQUIRED")
                secret_lease = self.secret_manager.resolve_secret(
                    secret_ref,
                    provider_id=provider_id,
                    purpose="provider_execution",
                    tenant_id=tenant_id,
                    principal_id=principal_id,
                )
                options["provider_credential_lease"] = secret_lease
            except Exception as exc:
                if secret_lease is not None:
                    secret_lease.close()
                released = self.release_unstarted_request(
                    project_id=project_id, provider_id=provider_id,
                    request_id=request_id, tenant_id=tenant_id,
                )
                safe_status = getattr(exc, "status", None)
                if not isinstance(safe_status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", safe_status):
                    safe_status = "MANAGED_SECRET_RESOLUTION_FAILED"
                return {
                    "status": safe_status,
                    "executed": False,
                    "provider": provider_id,
                    "release": released,
                }
        started_at = time.monotonic()
        try:
            adapter_result = adapter(**options)
        except Exception as exc:
            # The adapter may have transmitted the request before failing. Do not
            # release cost, quota, or credit reservations on this ambiguous outcome.
            # Extract only structured, explicitly exposed attributes; never return
            # exception text or provider response bodies.
            status_code = getattr(exc, "status_code", None)
            provider_code = getattr(exc, "provider_error_code", None)
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            provider_error = None
            if status_code is not None:
                provider_error = classify_provider_error(
                    status_code,
                    provider_id=provider_id,
                    provider_code=provider_code,
                    retry_after_seconds=retry_after_seconds,
                )
            reason = (
                provider_error.get("status")
                if isinstance(provider_error, dict)
                else "EXECUTION_OUTCOME_UNKNOWN"
            )
            incident_result = None
            if (
                self.incident_manager is not None
                and isinstance(provider_error, dict)
            ):
                incident_category = provider_error.get("category")
                severity = (
                    "critical"
                    if incident_category in {"billing", "credits_exhausted"}
                    else "warning"
                    if incident_category == "transient"
                    else "error"
                )
                try:
                    incident_result = self.incident_manager.record(
                        provider_id=provider_id,
                        category=category,
                        severity=severity,
                        code=reason,
                    )
                except Exception as incident_exc:
                    incident_result = {
                        "status": "INCIDENT_RECORD_FAILED",
                        "saved": False,
                        "error_type": type(incident_exc).__name__,
                    }
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "provider": provider_id,
                "request_id": request_id,
                "reservations_retained": True,
                "error_type": type(exc).__name__,
                "provider_error": provider_error,
                "incident": incident_result,
                "retry_decision": {"retry": False, "reason": reason},
            }
        finally:
            if secret_lease is not None:
                options.pop("provider_credential_lease", None)
                secret_lease.close()
                secret_lease = None
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
                project_id=project_id, provider_id=provider_id, request_id=request_id, tenant_id=tenant_id
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
        slo_result = None
        if self.slo_manager is not None:
            if not self._valid_amount(latency_ms):
                slo_result = {"status": "SLO_LATENCY_UNAVAILABLE", "saved": False}
            else:
                try:
                    slo_result = self.slo_manager.record(
                        provider_id=provider_id,
                        category=category,
                        success=final_success,
                        latency_ms=latency_ms,
                    )
                except Exception as exc:
                    slo_result = {
                        "status": "SLO_OBSERVATION_FAILED",
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
                provider_id=provider_id,
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
            "slo_observation": slo_result,
            "degraded_mode": degraded_mode_state,
            "benchmark": benchmark_result,
            "incident": incident_result,
            "provider_error": provider_error,
            "budget_alerts": budget_alerts,
            "authorization": authorization,
            "feature_flag": feature_flag_result,
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
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Account for a started request without assuming ambiguous failures were free.

        charge_outcome is consulted when execution failed with zero reported cost:
        not_incurred must be an explicit provider-confirmed no-charge outcome;
        unknown retains the cost reservation; incurred flags missing usage data.
        Quota is committed for every started request, including failed requests.
        """
        if not all(isinstance(value, str) and value.strip() for value in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "finalized": False}
        if self.tenant_isolation_manager is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            return {"status": "TENANT_CONTEXT_REQUIRED", "finalized": False}
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
                quota_result = (
                    self.quota_manager.commit(request_id=request_id, tenant_id=tenant_id)
                    if tenant_id is not None
                    else self.quota_manager.commit(request_id=request_id)
                )
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
        self, *, project_id: str, provider_id: str, request_id: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Release reservations only when the caller knows no provider request started."""
        if not all(isinstance(value, str) and value.strip() for value in (project_id, provider_id, request_id)):
            return {"status": "INVALID_USAGE_RECORD", "released": False}
        if self.tenant_isolation_manager is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            return {"status": "TENANT_CONTEXT_REQUIRED", "released": False}
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
                    results["quota"] = (
                        release(request_id=request_id, provider_id=provider_id, tenant_id=tenant_id)
                        if tenant_id is not None
                        else release(request_id=request_id, provider_id=provider_id)
                    )
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
