"""Safe registration for the project's image-generation provider adapters."""
from __future__ import annotations

import importlib
from pathlib import PurePosixPath
from typing import Any


SUPPORTED_INTERFACE_VERSION = "1.0"
TRUSTED_PROVIDER_PREFIX = "projects/generation/providers/"


class ProviderManager:
    def __init__(self):
        self.providers: dict[str, Any] = {}

    @staticmethod
    def _module_from_path(module_path: str) -> str:
        """Resolve only Python modules inside the trusted generation-provider package."""
        if not isinstance(module_path, str) or not module_path or "\\" in module_path:
            raise ValueError("Invalid provider module path")
        path = PurePosixPath(module_path)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ValueError("Invalid provider module path")
        normalized = path.as_posix()
        if not normalized.startswith(TRUSTED_PROVIDER_PREFIX) or not normalized.endswith(".py"):
            raise ValueError("Provider module is outside the trusted package")
        parts = path.parts
        if any(not part.replace("_", "").replace("-", "").isalnum() for part in parts[:-1]):
            raise ValueError("Invalid provider module path")
        module_parts = list(parts[:-1]) + [path.stem]
        return ".".join(module_parts)

    @staticmethod
    def _validate_instance(provider: Any) -> None:
        try:
            version = getattr(provider, "PROVIDER_INTERFACE_VERSION", SUPPORTED_INTERFACE_VERSION)
        except Exception:
            # Do not expose arbitrary adapter property exceptions or their details.
            raise ValueError("Provider interface version could not be read") from None
        if not isinstance(version, str) or version != SUPPORTED_INTERFACE_VERSION:
            raise ValueError("Unsupported provider interface version")
        try:
            generate_image = getattr(provider, "generate_image", None)
        except Exception:
            raise ValueError("Provider image-generation contract could not be read") from None
        if not callable(generate_image):
            raise ValueError("Provider must implement callable generate_image")

    def _build_provider(self, name: str, module_name: str, class_name: str) -> Any:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Invalid provider name")
        if not isinstance(module_name, str) or not module_name.startswith("projects.generation.providers."):
            raise ValueError("Provider module is outside the trusted package")
        if not isinstance(class_name, str) or not class_name.isidentifier():
            raise ValueError("Invalid provider class name")
        try:
            module = importlib.import_module(module_name)
            provider_class = getattr(module, class_name)
            provider = provider_class()
        except Exception:
            raise ValueError("Provider adapter could not be loaded") from None
        self._validate_instance(provider)
        return provider

    def register(self, name, module_name, class_name):
        provider = self._build_provider(name, module_name, class_name)
        self.providers[name] = provider
        return provider

    def load_registry(self, registry):
        if not isinstance(registry, dict):
            raise ValueError("Invalid provider registry")
        image_section = registry.get("image", {})
        modules = registry.get("provider_modules")
        if modules is None and isinstance(image_section, dict):
            modules = image_section.get("provider_modules", {})
        if not isinstance(modules, dict):
            raise ValueError("Invalid provider module registry")
        candidate = {}
        for name, module_path in modules.items():
            module_name = self._module_from_path(module_path)
            class_name = name.replace(" ", "") + "Provider" if isinstance(name, str) else ""
            candidate[name] = self._build_provider(name, module_name, class_name)
        # Atomic activation: preserve the previous live registry if any entry fails.
        self.providers = candidate
        return {"status": "LOADED", "providers": sorted(candidate)}

    def get(self, name):
        return self.providers.get(name)
