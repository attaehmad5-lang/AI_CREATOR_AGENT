"""Safe runtime registry for explicitly injected provider generation adapters.

The registry does not implement vendor APIs. It binds an application-supplied callable
to a catalog provider and refuses dispatch until that provider is enabled and verified.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Callable


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\Z")


class ProviderAdapterRegistry:
    """Thread-safe adapter registry with verified-provider and capability gates."""

    def __init__(self, connection_manager: Any) -> None:
        if not callable(getattr(connection_manager, "get_provider", None)):
            raise ValueError("connection_manager must support get_provider")
        if not callable(getattr(connection_manager, "get_state", None)):
            raise ValueError("connection_manager must support get_state")
        self.connection_manager = connection_manager
        self._lock = threading.RLock()
        self._adapters: dict[str, dict[str, Any]] = {}

    def register(
        self,
        provider_id: str,
        adapter: Callable[..., Any],
        *,
        modalities: list[str] | tuple[str, ...],
        model_ids: list[str] | tuple[str, ...] | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if not callable(adapter):
            return {"status": "INVALID_ADAPTER", "registered": False}
        if not isinstance(replace, bool):
            return {"status": "INVALID_REPLACE_FLAG", "registered": False}
        if not isinstance(modalities, (list, tuple)) or not modalities:
            return {"status": "INVALID_MODALITIES", "registered": False}
        normalized_modalities: set[str] = set()
        for modality in modalities:
            if not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality):
                return {"status": "INVALID_MODALITIES", "registered": False}
            normalized_modalities.add(modality)
        if model_ids is not None:
            if not isinstance(model_ids, (list, tuple)):
                return {"status": "INVALID_MODEL_IDS", "registered": False}
            if any(not isinstance(model, str) or not _IDENTIFIER.fullmatch(model) for model in model_ids):
                return {"status": "INVALID_MODEL_IDS", "registered": False}
            normalized_models = set(model_ids)
        else:
            normalized_models = None
        provider = self.connection_manager.get_provider(provider_id)
        if provider is None:
            return {"status": "PROVIDER_NOT_FOUND", "registered": False}
        with self._lock:
            if provider_id in self._adapters and not replace:
                return {"status": "ADAPTER_ALREADY_REGISTERED", "registered": False}
            self._adapters[provider_id] = {
                "adapter": adapter,
                "modalities": frozenset(normalized_modalities),
                "model_ids": None if normalized_models is None else frozenset(normalized_models),
            }
        return {"status": "ADAPTER_REGISTERED", "registered": True, "provider_id": provider_id}

    def unregister(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "unregistered": False}
        with self._lock:
            if provider_id not in self._adapters:
                return {"status": "ADAPTER_NOT_REGISTERED", "unregistered": False}
            del self._adapters[provider_id]
        return {"status": "ADAPTER_UNREGISTERED", "unregistered": True, "provider_id": provider_id}

    def list_adapters(self) -> dict[str, Any]:
        with self._lock:
            entries = [
                {
                    "provider_id": provider_id,
                    "modalities": sorted(entry["modalities"]),
                    "model_ids": sorted(entry["model_ids"]) if entry["model_ids"] is not None else None,
                }
                for provider_id, entry in sorted(self._adapters.items())
            ]
        return {"status": "ADAPTERS_AVAILABLE" if entries else "NO_ADAPTERS", "count": len(entries), "adapters": entries}

    def resolve(
        self, provider_id: str, *, modality: str, model_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "resolved": False}
        if not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality):
            return {"status": "INVALID_MODALITY", "resolved": False}
        if model_id is not None and (not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id)):
            return {"status": "INVALID_MODEL_ID", "resolved": False}
        provider = self.connection_manager.get_provider(provider_id)
        if provider is None:
            return {"status": "PROVIDER_NOT_FOUND", "resolved": False}
        if provider.get("enabled") is not True:
            return {"status": "PROVIDER_DISABLED", "resolved": False}
        state = self.connection_manager.get_state(provider_id)
        if not isinstance(state, dict) or state.get("status") != "CONNECTED" or state.get("verified") is not True:
            return {"status": "PROVIDER_NOT_VERIFIED", "resolved": False}
        with self._lock:
            entry = self._adapters.get(provider_id)
        if entry is None:
            return {"status": "ADAPTER_NOT_REGISTERED", "resolved": False}
        if modality not in entry["modalities"]:
            return {"status": "MODALITY_UNSUPPORTED", "resolved": False}
        if entry["model_ids"] is not None and model_id not in entry["model_ids"]:
            return {"status": "MODEL_UNSUPPORTED", "resolved": False}
        catalog_models = provider.get("supported_models")
        if model_id is not None and isinstance(catalog_models, list) and catalog_models and model_id not in catalog_models:
            return {"status": "MODEL_NOT_IN_CATALOG", "resolved": False}
        return {"status": "ADAPTER_RESOLVED", "resolved": True, "adapter": entry["adapter"], "provider_id": provider_id}

    def execute(
        self,
        provider_id: str,
        *,
        modality: str,
        request_id: str,
        model_id: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        reserved = {"provider_id", "modality", "request_id", "model_id"}
        if reserved.intersection(options):
            return {"status": "RESERVED_ADAPTER_OPTION", "executed": False}
        resolved = self.resolve(provider_id, modality=modality, model_id=model_id)
        if resolved.get("resolved") is not True:
            return {"status": resolved["status"], "executed": False}
        try:
            result = resolved["adapter"](
                provider_id=provider_id,
                modality=modality,
                request_id=request_id,
                model_id=model_id,
                **options,
            )
        except Exception as exc:
            return {
                "status": "ADAPTER_EXECUTION_FAILED",
                "executed": False,
                "provider_id": provider_id,
                "request_id": request_id,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or not isinstance(result.get("started"), bool):
            return {
                "status": "ADAPTER_RESPONSE_INVALID",
                "executed": False,
                "provider_id": provider_id,
                "request_id": request_id,
            }
        return {
            "status": "ADAPTER_EXECUTED",
            "executed": True,
            "provider_id": provider_id,
            "request_id": request_id,
            "result": result,
        }
