"""Allowlisted provider runtime with interface checks, in-flight tracking and safe unload."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .errors import NotFoundError, ValidationError


class ProviderRuntime:
    """Loads only explicitly registered factories; never imports module names from catalog data."""

    def __init__(self):
        self._factories: dict[str, Callable[[dict], Any]] = {}
        self._instances: dict[str, Any] = {}
        self._inflight: dict[str, int] = {}
        self._draining: set[str] = set()
        self._condition = threading.Condition(threading.RLock())

    def register_factory(self, provider_id: str, factory: Callable[[dict], Any]) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip() or not callable(factory):
            raise ValidationError("provider_id and factory are required")
        with self._condition:
            if provider_id in self._instances:
                raise ValidationError("cannot replace a factory while provider is loaded")
            self._factories[provider_id] = factory

    def load(self, provider_id: str, config: Optional[dict] = None) -> dict:
        config = {} if config is None else config
        if not isinstance(config, dict):
            raise ValidationError("provider config must be an object")
        with self._condition:
            if provider_id in self._instances:
                return {"provider_id": provider_id, "loaded": True, "already_loaded": True}
            factory = self._factories.get(provider_id)
            if factory is None:
                raise NotFoundError(f"provider factory is not allowlisted: {provider_id}")
            instance = factory(dict(config))
            if not callable(getattr(instance, "generate", None)):
                close = getattr(instance, "close", None)
                if callable(close):
                    close()
                raise ValidationError("provider instance must implement generate(...)")
            self._instances[provider_id] = instance
            self._inflight[provider_id] = 0
            self._draining.discard(provider_id)
            return {"provider_id": provider_id, "loaded": True, "already_loaded": False}

    def status(self, provider_id: str) -> dict:
        with self._condition:
            instance = self._instances.get(provider_id)
            if instance is None:
                return {"provider_id": provider_id, "loaded": False, "inflight": 0, "draining": False}
            return {
                "provider_id": provider_id, "loaded": True,
                "inflight": self._inflight.get(provider_id, 0),
                "draining": provider_id in self._draining,
                "health_check_supported": callable(getattr(instance, "health_check", None)),
            }

    def invoke(self, provider_id: str, operation: str, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(operation, str) or not operation or operation.startswith("_"):
            raise ValidationError("operation must be a public method name")
        with self._condition:
            instance = self._instances.get(provider_id)
            if instance is None:
                raise NotFoundError(f"provider is not loaded: {provider_id}")
            if provider_id in self._draining:
                raise ValidationError("provider is draining and does not accept new calls")
            method = getattr(instance, operation, None)
            if not callable(method):
                raise ValidationError(f"provider operation is unavailable: {operation}")
            self._inflight[provider_id] += 1
        try:
            return method(*args, **kwargs)
        finally:
            with self._condition:
                self._inflight[provider_id] -= 1
                self._condition.notify_all()

    def unload(self, provider_id: str, timeout: float = 0.0) -> dict:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
            raise ValidationError("timeout must be >= 0")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            instance = self._instances.get(provider_id)
            if instance is None:
                return {"provider_id": provider_id, "loaded": False, "already_unloaded": True}
            self._draining.add(provider_id)
            while self._inflight.get(provider_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._draining.discard(provider_id)
                    self._condition.notify_all()
                    raise TimeoutError("provider still has in-flight calls")
                self._condition.wait(remaining)
            self._instances.pop(provider_id, None)
            self._inflight.pop(provider_id, None)
            self._draining.discard(provider_id)
        close = getattr(instance, "close", None)
        if callable(close):
            close()
        return {"provider_id": provider_id, "loaded": False, "already_unloaded": False}
