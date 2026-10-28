"""Read-only, deterministic discovery for models declared by provider catalogs.

This layer reports catalog metadata only; it does not imply that a model is reachable,
available to the user's account, or billable at the listed terms.
"""
from __future__ import annotations

import re
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}\Z")
_PUBLIC_MODEL_FIELDS = {
    "id",
    "name",
    "display_name",
    "description",
    "version",
    "capabilities",
    "modalities",
    "input_modalities",
    "output_modalities",
    "context_length",
    "max_output_tokens",
    "deprecated",
}


def _contains_credential_field(value: Any) -> bool:
    forbidden = {
        "api_key", "apikey", "api_token", "token", "access_token",
        "refresh_token", "password", "secret", "credential",
        "credentials", "private_key", "authorization", "bearer_token",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in forbidden or any(
                marker in normalized for marker in ("password", "secret", "credential", "api_key", "access_token", "refresh_token")
            ):
                if normalized not in {"api_key_env", "token_env", "credential_env", "secret_ref"}:
                    return True
            if _contains_credential_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_credential_field(child) for child in value)
    return False


class ModelCatalog:
    """Exact-match model discovery backed by a provider connection manager."""

    def __init__(self, connection_manager: Any) -> None:
        if not callable(getattr(connection_manager, "list_providers", None)):
            raise ValueError("connection_manager must provide list_providers()")
        self.connection_manager = connection_manager

    @staticmethod
    def _public_model(provider: dict[str, Any], model: Any) -> dict[str, Any] | None:
        if not isinstance(model, dict):
            return None
        model_id = model.get("id")
        if not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id):
            return None
        if _contains_credential_field(model):
            return None
        public = {key: model[key] for key in _PUBLIC_MODEL_FIELDS if key in model}
        if "name" not in public:
            public["name"] = public.get("display_name", model_id)
        public["provider_id"] = provider.get("id")
        public["provider_name"] = provider.get("name")
        public["category"] = provider.get("category")
        public["provider_enabled"] = provider.get("enabled") is True
        return public

    def list_models(
        self,
        *,
        provider_id: str | None = None,
        capability: str | None = None,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        if provider_id is not None and (
            not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id)
        ):
            return {"status": "INVALID_PROVIDER_ID", "models": [], "count": 0}
        if capability is not None and (
            not isinstance(capability, str) or not _IDENTIFIER.fullmatch(capability)
        ):
            return {"status": "INVALID_CAPABILITY", "models": [], "count": 0}
        if not isinstance(include_disabled, bool):
            return {"status": "INVALID_INCLUDE_DISABLED", "models": [], "count": 0}

        providers = self.connection_manager.list_providers()
        models: list[dict[str, Any]] = []
        matched_provider = provider_id is None
        for provider in providers:
            if provider_id is not None and provider.get("id") != provider_id:
                continue
            matched_provider = True
            if provider.get("enabled") is not True and not include_disabled:
                continue
            supported = provider.get("supported_models", [])
            if not isinstance(supported, list):
                continue
            for item in supported:
                model = self._public_model(provider, item)
                if model is None:
                    continue
                capabilities = model.get("capabilities", [])
                if capability is not None:
                    if not isinstance(capabilities, list) or not any(
                        isinstance(value, str) and value.casefold() == capability.casefold()
                        for value in capabilities
                    ):
                        continue
                models.append(model)
        if not matched_provider:
            return {"status": "PROVIDER_NOT_FOUND", "models": [], "count": 0}
        models.sort(key=lambda item: (
            str(item.get("provider_id", "")).casefold(),
            str(item.get("id", "")).casefold(),
        ))
        return {
            "status": "MODELS_AVAILABLE" if models else "NO_MODELS",
            "models": models,
            "count": len(models),
            "source": "catalog_metadata_only",
        }

    def find_model(
        self, model_id: str, *, provider_id: str | None = None,
        include_disabled: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id):
            return {"status": "INVALID_MODEL_ID", "matches": [], "count": 0}
        result = self.list_models(
            provider_id=provider_id, include_disabled=include_disabled
        )
        if result["status"] == "PROVIDER_NOT_FOUND":
            return {"status": result["status"], "matches": [], "count": 0}
        matches = [
            model for model in result["models"]
            if model["id"].casefold() == model_id.casefold()
        ]
        return {
            "status": "MODEL_FOUND" if matches else "MODEL_NOT_FOUND",
            "matches": matches,
            "count": len(matches),
            "source": "catalog_metadata_only",
        }
