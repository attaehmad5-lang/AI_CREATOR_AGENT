"""Safe, atomic loader for the project's explicitly trusted provider adapters."""
from __future__ import annotations

import importlib
import re
from typing import Any

_PROVIDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ]{0,63}\Z")
_MODULE_NAME = re.compile(r"projects\.generation\.providers(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z")
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*Provider\Z")
_PATH_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ProviderManager:
    """Load only provider modules inside the trusted generation.providers package.

    A catalog is configuration, not permission to import arbitrary Python modules.
    All entries are validated and instantiated before the active registry is changed.
    """

    def __init__(self):
        self.providers: dict[str, Any] = {}

    @staticmethod
    def _validate_spec(name: Any, module_name: Any, class_name: Any) -> None:
        if not isinstance(name, str) or not _PROVIDER_NAME.fullmatch(name):
            raise ValueError("invalid provider name")
        if not isinstance(module_name, str) or not _MODULE_NAME.fullmatch(module_name):
            raise ValueError("provider module is outside the trusted package")
        if not isinstance(class_name, str) or not _CLASS_NAME.fullmatch(class_name):
            raise ValueError("invalid provider class name")
        if class_name != name.replace(" ", "") + "Provider":
            raise ValueError("provider class does not match its registry name")

    @staticmethod
    def _module_from_path(module_path: Any) -> str:
        if (
            not isinstance(module_path, str)
            or not module_path.endswith(".py")
            or "\\" in module_path
            or module_path.startswith("/")
        ):
            raise ValueError("invalid provider module path")
        parts = module_path[:-3].split("/")
        if (
            len(parts) < 4
            or parts[:3] != ["projects", "generation", "providers"]
            or any(part in {"", ".", ".."} or not _PATH_PART.fullmatch(part) for part in parts)
        ):
            raise ValueError("provider module path is outside the trusted package")
        return ".".join(parts)

    def register(self, name, module_name, class_name):
        self._validate_spec(name, module_name, class_name)
        try:
            module = importlib.import_module(module_name)
            provider_class = getattr(module, class_name)
            if not callable(provider_class):
                raise TypeError("provider factory is not callable")
            instance = provider_class()
        except Exception as exc:
            raise ValueError("provider registration failed") from exc
        self.providers[name] = instance
        return instance

    def load_registry(self, registry):
        if not isinstance(registry, dict):
            raise ValueError("provider registry must be an object")
        modules = registry.get("provider_modules", {})
        if not isinstance(modules, dict):
            raise ValueError("provider_modules must be an object")

        specs = []
        seen_modules = set()
        for name, module_path in modules.items():
            module_name = self._module_from_path(module_path)
            class_name = name.replace(" ", "") + "Provider" if isinstance(name, str) else ""
            self._validate_spec(name, module_name, class_name)
            if module_name in seen_modules:
                raise ValueError("duplicate provider module")
            seen_modules.add(module_name)
            specs.append((name, module_name, class_name))

        # Stage every instance first; a bad later entry cannot leave a half-loaded
        # registry or replace the currently working provider set. On success, replace
        # the active set so removed providers cannot linger as stale runtime entries.
        staged = {}
        try:
            for name, module_name, class_name in specs:
                module = importlib.import_module(module_name)
                provider_class = getattr(module, class_name)
                if not callable(provider_class):
                    raise TypeError("provider factory is not callable")
                staged[name] = provider_class()
        except Exception as exc:
            raise ValueError("provider registry loading failed") from exc
        self.providers = staged
        return {"loaded": sorted(staged), "count": len(staged)}

    def get(self, name):
        return self.providers.get(name)
