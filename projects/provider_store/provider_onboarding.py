"""Fail-closed provider onboarding helpers.

The workflow validates metadata before persistence, keeps new providers disabled,
and treats a provider-specific probe as separate evidence rather than guessing that
registration means connectivity.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Callable
from urllib.parse import urlsplit

_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_TIMEOUT_MAX = 30.0


class ProviderOnboardingWorkflow:
    def __init__(self, connection_manager: Any) -> None:
        if not callable(getattr(connection_manager, "register_provider", None)):
            raise ValueError("connection manager must support provider registration")
        if not callable(getattr(connection_manager, "validate_catalog_schema", None)):
            raise ValueError("connection manager must validate catalog schemas")
        self.connection_manager = connection_manager

    @staticmethod
    def _validate_documentation_url(value: Any) -> bool:
        if not isinstance(value, str) or len(value) > 2048:
            return False
        try:
            parsed = urlsplit(value)
            return (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.query == ""
                and parsed.fragment == ""
                and not any(ord(ch) < 32 or ch.isspace() for ch in value)
            )
        except (TypeError, ValueError):
            return False

    def prepare_candidate(self, provider: Any) -> dict[str, Any]:
        """Validate candidate metadata without writing or enabling a provider."""
        if not isinstance(provider, dict):
            return {"status": "INVALID_PROVIDER", "prepared": False}
        try:
            candidate = deepcopy(provider)
        except Exception:
            return {"status": "INVALID_PROVIDER", "prepared": False}
        provider_id = candidate.get("id")
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "prepared": False}
        if getattr(self.connection_manager, "_contains_credential_value_field", lambda _: True)(candidate):
            return {"status": "SENSITIVE_METADATA_REJECTED", "prepared": False}
        category = candidate.get("category")
        connection_type = candidate.get("connection_type")
        capabilities = candidate.get("capabilities")
        if not isinstance(category, str) or not category.strip():
            return {"status": "INVALID_CATEGORY", "prepared": False}
        if not isinstance(connection_type, str) or not connection_type.strip():
            return {"status": "INVALID_CONNECTION_TYPE", "prepared": False}
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) or not _CAPABILITY.fullmatch(item) for item in capabilities)
            or len(capabilities) != len(set(capabilities))
        ):
            return {"status": "INVALID_CAPABILITIES", "prepared": False}
        if connection_type in {"API", "API_OR_ACCOUNT", "ACCOUNT_OR_API"}:
            if not self._validate_documentation_url(candidate.get("official_api_url")):
                return {"status": "OFFICIAL_HTTPS_DOCUMENTATION_REQUIRED", "prepared": False}
        candidate["enabled"] = False
        candidate["status"] = "LOCKED"
        try:
            catalog = deepcopy(getattr(self.connection_manager, "catalog"))
            providers = catalog.get("providers")
            if not isinstance(providers, list):
                return {"status": "INVALID_CATALOG", "prepared": False}
            if any(isinstance(item, dict) and item.get("id") == provider_id for item in providers):
                return {"status": "ALREADY_EXISTS", "prepared": False}
            catalog["providers"] = providers + [candidate]
            validation = self.connection_manager.validate_catalog_schema(catalog)
        except Exception as exc:
            return {
                "status": "CATALOG_VALIDATION_FAILED",
                "prepared": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return {"status": "INVALID_PROVIDER_SCHEMA", "prepared": False}
        return {"status": "PREPARED_DISABLED", "prepared": True, "provider": candidate}

    def verify_candidate(
        self,
        provider: Any,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Run a trusted provider-specific probe without persisting or enabling."""
        prepared = self.prepare_candidate(provider)
        if prepared.get("prepared") is not True:
            return {"status": prepared.get("status", "CANDIDATE_BLOCKED"), "verified": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "verified": False}
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > _TIMEOUT_MAX
        ):
            return {"status": "INVALID_TIMEOUT", "verified": False}
        try:
            result = probe(prepared["provider"], timeout_seconds=float(timeout_seconds))
        except Exception as exc:
            return {"status": "PROBE_FAILED", "verified": False, "error_type": type(exc).__name__}
        verified = result is True or (isinstance(result, dict) and result.get("verified") is True)
        return {
            "status": "VERIFIED" if verified else "PROBE_NOT_VERIFIED",
            "verified": verified,
            "provider": prepared["provider"]["id"],
        }

    def register_candidate(self, provider: Any) -> dict[str, Any]:
        """Persist a valid candidate as disabled; never activate it implicitly."""
        prepared = self.prepare_candidate(provider)
        if prepared.get("prepared") is not True:
            return {"status": prepared.get("status", "CANDIDATE_BLOCKED"), "saved": False}
        try:
            result = self.connection_manager.register_provider(prepared["provider"])
        except Exception as exc:
            return {"status": "REGISTRATION_FAILED", "saved": False, "error_type": type(exc).__name__}
        if not isinstance(result, dict) or result.get("saved") is not True:
            return {"status": "REGISTRATION_FAILED", "saved": False}
        return {
            "status": "REGISTERED_DISABLED",
            "saved": True,
            "provider": prepared["provider"]["id"],
            "enabled": False,
        }
