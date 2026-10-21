"""Allowlisted provider plugin lifecycle without arbitrary module imports."""
from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from typing import Any, Callable

from .connection_manager import ConnectionManager

_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")


class ProviderPluginManager:
    """Manage trusted in-process provider factories through a strict allowlist.

    This layer deliberately accepts callable objects only; it never imports a
    module/class from catalog-controlled strings. Package signing, process
    isolation, and resource quotas remain responsibilities of a future sandbox.
    """

    def __init__(
        self,
        connection_manager: ConnectionManager,
        *,
        approved_plugin_ids: Iterable[str],
    ) -> None:
        if not all(callable(getattr(connection_manager, name, None)) for name in (
            "provider_exists", "load_provider", "unload_provider", "get_loaded_provider"
        )):
            raise ValueError("connection_manager does not satisfy the required lifecycle contract")
        if isinstance(approved_plugin_ids, (str, bytes)):
            raise ValueError("approved_plugin_ids must be a collection of provider IDs")
        try:
            ids = list(approved_plugin_ids)
        except TypeError as exc:
            raise ValueError("approved_plugin_ids must be iterable") from exc
        if len(ids) > 100:
            raise ValueError("approved_plugin_ids exceeds the safe limit")
        if any(not isinstance(item, str) or not _PROVIDER_ID_RE.fullmatch(item) for item in ids):
            raise ValueError("approved_plugin_ids contains an invalid provider ID")
        if len(set(ids)) != len(ids):
            raise ValueError("approved_plugin_ids contains duplicates")
        self.connection_manager = connection_manager
        self._approved_ids = frozenset(ids)
        self._factories: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._lock = threading.RLock()

    def register_factory(self, provider_id: str, factory: Any) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if provider_id not in self._approved_ids:
            return {"status": "PLUGIN_NOT_APPROVED", "provider": provider_id, "registered": False}
        if not callable(factory):
            return {"status": "INVALID_FACTORY", "provider": provider_id, "registered": False}
        if not self.connection_manager.provider_exists(provider_id):
            return {"status": "PROVIDER_NOT_FOUND", "provider": provider_id, "registered": False}
        with self._lock:
            if provider_id in self._factories:
                return {"status": "FACTORY_ALREADY_REGISTERED", "provider": provider_id, "registered": False}
            self._factories[provider_id] = factory
        return {"status": "FACTORY_REGISTERED", "provider": provider_id, "registered": True}

    def load(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "loaded": False}
        if provider_id not in self._approved_ids:
            return {"status": "PLUGIN_NOT_APPROVED", "provider": provider_id, "loaded": False}
        with self._lock:
            factory = self._factories.get(provider_id)
            if factory is None:
                return {"status": "PLUGIN_FACTORY_NOT_REGISTERED", "provider": provider_id, "loaded": False}
            # Serialize factory execution and runtime registration against
            # unload/unregister so an active load cannot lose its factory.
            return self.connection_manager.load_provider(provider_id, factory)

    def unload(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "unloaded": False}
        if provider_id not in self._approved_ids:
            return {"status": "PLUGIN_NOT_APPROVED", "provider": provider_id, "unloaded": False}
        with self._lock:
            return self.connection_manager.unload_provider(provider_id)

    def unregister_factory(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "unregistered": False}
        if provider_id not in self._approved_ids:
            return {"status": "PLUGIN_NOT_APPROVED", "provider": provider_id, "unregistered": False}
        with self._lock:
            if self.connection_manager.get_loaded_provider(provider_id) is not None:
                return {"status": "PLUGIN_ACTIVE", "provider": provider_id, "unregistered": False}
            if provider_id not in self._factories:
                return {"status": "PLUGIN_FACTORY_NOT_REGISTERED", "provider": provider_id, "unregistered": False}
            del self._factories[provider_id]
        return {"status": "FACTORY_UNREGISTERED", "provider": provider_id, "unregistered": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            registered = sorted(self._factories)
        loaded = sorted(
            provider_id for provider_id in registered
            if self.connection_manager.get_loaded_provider(provider_id) is not None
        )
        return {
            "status": "READY",
            "approved_plugin_ids": sorted(self._approved_ids),
            "registered_plugin_ids": registered,
            "loaded_plugin_ids": loaded,
            "arbitrary_module_imports_allowed": False,
            "process_isolation_enabled": False,
        }
