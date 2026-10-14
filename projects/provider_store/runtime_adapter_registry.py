"""Trusted runtime adapter registry for the central provider catalog.

Catalog metadata never names an importable Python module or class. Built-in
factories are explicitly allowlisted here; extension factories must be supplied
by trusted application code and are never loaded from provider JSON.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .connection_manager import ConnectionManager

ProviderFactory = Callable[[dict[str, Any]], Any]


def _huggingface_factory(_: dict[str, Any]) -> Any:
    from projects.generation.providers.huggingface_provider import HuggingFaceProvider
    return HuggingFaceProvider()


def _pollinations_factory(_: dict[str, Any]) -> Any:
    from projects.generation.providers.pollinations_provider import PollinationsProvider
    return PollinationsProvider()


BUILTIN_PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "huggingface": _huggingface_factory,
    "pollinations": _pollinations_factory,
}


class RuntimeProviderRegistry:
    """Bridge catalog entries to trusted factories and ConnectionManager lifecycle APIs."""

    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        factories: dict[str, ProviderFactory] | None = None,
    ) -> None:
        if not callable(getattr(connection_manager, "get_provider", None)):
            raise ValueError("connection_manager must expose get_provider")
        if not callable(getattr(connection_manager, "load_provider", None)):
            raise ValueError("connection_manager must expose load_provider")
        if not callable(getattr(connection_manager, "unload_provider", None)):
            raise ValueError("connection_manager must expose unload_provider")
        if factories is not None and not isinstance(factories, dict):
            raise ValueError("factories must be a mapping of provider IDs to trusted callables")
        supplied = BUILTIN_PROVIDER_FACTORIES if factories is None else factories
        validated: dict[str, ProviderFactory] = {}
        for provider_id, factory in supplied.items():
            if (
                not isinstance(provider_id, str)
                or not provider_id
                or len(provider_id) > 100
                or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in provider_id)
            ):
                raise ValueError("factory registry contains an invalid provider ID")
            if not callable(factory):
                raise ValueError(f"factory for {provider_id} must be callable")
            validated[provider_id] = factory
        self.connection_manager = connection_manager
        self._factories = validated
        self._lock = threading.RLock()

    def registered_ids(self) -> list[str]:
        """Return a deterministic list of trusted adapter IDs, not all catalog providers."""
        with self._lock:
            return sorted(self._factories)

    def register_factory(
        self, provider_id: str, factory: ProviderFactory, *, replace: bool = False
    ) -> dict[str, Any]:
        """Register an application-supplied factory for an existing catalog provider."""
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 100
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in provider_id)
        ):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if not callable(factory):
            return {"status": "INVALID_FACTORY", "provider": provider_id, "registered": False}
        provider = self.connection_manager.get_provider(provider_id)
        if provider is None:
            return {"status": "PROVIDER_NOT_FOUND", "provider": provider_id, "registered": False}
        with self._lock:
            if provider_id in self._factories and not replace:
                return {"status": "FACTORY_ALREADY_REGISTERED", "provider": provider_id, "registered": False}
            loaded = self.connection_manager.get_loaded_provider(provider_id) if callable(
                getattr(self.connection_manager, "get_loaded_provider", None)
            ) else None
            if loaded is not None:
                return {"status": "PROVIDER_ALREADY_LOADED", "provider": provider_id, "registered": False}
            self._factories[provider_id] = factory
        return {"status": "FACTORY_REGISTERED", "provider": provider_id, "registered": True}

    def unregister_factory(self, provider_id: str) -> dict[str, Any]:
        """Remove a factory only when its runtime instance is not loaded."""
        if not isinstance(provider_id, str) or not provider_id:
            return {"status": "INVALID_PROVIDER_ID", "provider": provider_id, "unregistered": False}
        with self._lock:
            if provider_id not in self._factories:
                return {"status": "FACTORY_NOT_REGISTERED", "provider": provider_id, "unregistered": False}
            loaded = self.connection_manager.get_loaded_provider(provider_id) if callable(
                getattr(self.connection_manager, "get_loaded_provider", None)
            ) else None
            if loaded is not None:
                return {"status": "PROVIDER_ALREADY_LOADED", "provider": provider_id, "unregistered": False}
            del self._factories[provider_id]
        return {"status": "FACTORY_UNREGISTERED", "provider": provider_id, "unregistered": True}

    def load(self, provider_id: str) -> dict[str, Any]:
        """Load through ConnectionManager so enabled checks and modality contracts always apply."""
        if not isinstance(provider_id, str) or not provider_id:
            return {"status": "INVALID_PROVIDER_ID", "provider": provider_id, "loaded": False}
        with self._lock:
            factory = self._factories.get(provider_id)
        if factory is None:
            provider = self.connection_manager.get_provider(provider_id)
            if provider is None:
                return {"status": "NOT_FOUND", "provider": provider_id, "loaded": False}
            return {"status": "ADAPTER_NOT_REGISTERED", "provider": provider_id, "loaded": False}
        return self.connection_manager.load_provider(provider_id, factory)

    def load_all_enabled(self) -> dict[str, Any]:
        """Attempt all enabled catalog entries in stable ID order; never invent adapters."""
        discover = getattr(self.connection_manager, "discover_providers", None)
        if not callable(discover):
            return {"status": "DISCOVERY_UNAVAILABLE", "loaded": [], "results": []}
        providers = discover(enabled_only=True)
        if not isinstance(providers, list):
            return {"status": "INVALID_DISCOVERY_RESULT", "loaded": [], "results": []}
        results = [
            self.load(provider_id)
            for provider_id in sorted(
                item.get("id") for item in providers
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
        ]
        loaded = [item["provider"] for item in results if item.get("loaded") is True]
        failed = [item for item in results if item.get("loaded") is not True]
        return {
            "status": "LOADED" if not failed else ("PARTIAL" if loaded else "NO_ADAPTERS_LOADED"),
            "loaded": loaded,
            "results": results,
        }

    def unload(self, provider_id: str) -> dict[str, Any]:
        """Delegate cleanup and preserve manager error semantics."""
        return self.connection_manager.unload_provider(provider_id)

    def get(self, provider_id: str) -> Any | None:
        """Return only an already-loaded process-local instance."""
        getter = getattr(self.connection_manager, "get_loaded_provider", None)
        return getter(provider_id) if callable(getter) else None

    def status(self) -> dict[str, Any]:
        """Summarize catalog coverage without calling unsupported providers connected."""
        providers = self.connection_manager.list_providers()
        registered = set(self.registered_ids())
        catalog_ids = {
            item.get("id") for item in providers
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        enabled_ids = {
            item.get("id") for item in providers
            if isinstance(item, dict) and item.get("enabled") is True
            and isinstance(item.get("id"), str)
        }
        return {
            "catalog_provider_count": len(catalog_ids),
            "enabled_provider_count": len(enabled_ids),
            "registered_adapter_count": len(registered & catalog_ids),
            "enabled_without_adapter": sorted(enabled_ids - registered),
            "registered_adapter_ids": sorted(registered & catalog_ids),
            "status": "READY" if catalog_ids else "EMPTY",
        }
