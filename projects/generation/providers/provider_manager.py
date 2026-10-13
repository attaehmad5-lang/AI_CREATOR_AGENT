"""Allowlisted provider loader for the legacy generation facade.

Registry JSON may select known providers but may not choose arbitrary Python modules
or classes to import. New integrations should prefer ConnectionManager.load_provider
with an explicitly injected factory and capability contract.
"""
from __future__ import annotations

import importlib
from typing import Any


class ProviderManager:
    _ALLOWED_PROVIDERS = {
        "Hugging Face": (
            "projects.generation.providers.huggingface_provider",
            "HuggingFaceProvider",
            "projects/generation/providers/huggingface_provider.py",
        ),
        "Pollinations": (
            "projects.generation.providers.pollinations_provider",
            "PollinationsProvider",
            "projects/generation/providers/pollinations_provider.py",
        ),
        "Test Provider": (
            "projects.generation.providers.test_provider",
            "TestProvider",
            "projects/generation/providers/test_provider.py",
        ),
    }

    def __init__(self):
        self.providers: dict[str, Any] = {}

    def register(self, name: str, module_name: str | None = None, class_name: str | None = None) -> None:
        allowed = self._ALLOWED_PROVIDERS.get(name)
        if allowed is None:
            raise ValueError("provider name is not in the built-in allowlist")
        expected_module, expected_class, _ = allowed
        if module_name is not None and module_name != expected_module:
            raise ValueError("provider module does not match the built-in allowlist")
        if class_name is not None and class_name != expected_class:
            raise ValueError("provider class does not match the built-in allowlist")
        module = importlib.import_module(expected_module)
        provider_class = getattr(module, expected_class, None)
        if not callable(provider_class):
            raise ValueError("allowlisted provider class is unavailable")
        self.providers[name] = provider_class()

    def load_registry(self, registry: dict[str, Any]) -> None:
        if not isinstance(registry, dict):
            raise ValueError("provider registry must be an object")
        modules = registry.get("provider_modules", {})
        if not isinstance(modules, dict):
            raise ValueError("provider_modules must be an object")
        for name, module_path in modules.items():
            allowed = self._ALLOWED_PROVIDERS.get(name)
            if allowed is None:
                raise ValueError("registry contains a provider outside the built-in allowlist")
            expected_module, expected_class, expected_path = allowed
            if module_path != expected_path:
                raise ValueError("registry provider path does not match the built-in allowlist")
            self.register(name, expected_module, expected_class)

    def get(self, name: str) -> Any | None:
        return self.providers.get(name)
