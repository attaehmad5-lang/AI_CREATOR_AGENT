"""Version negotiation and atomic activation tests for generation provider adapters."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from projects.generation.providers.provider_manager import ProviderManager


class LegacyProvider:
    def generate_image(self, prompt, output_path):
        return None


class VersionedProvider:
    PROVIDER_INTERFACE_VERSION = "1.0"

    def generate_image(self, prompt, output_path):
        return None


class UnsupportedProvider:
    PROVIDER_INTERFACE_VERSION = "2.0"

    def generate_image(self, prompt, output_path):
        return None


class MalformedVersionProvider:
    PROVIDER_INTERFACE_VERSION = "1.0-beta"

    def generate_image(self, prompt, output_path):
        return None


class ProviderInterfaceVersionTests(unittest.TestCase):
    def test_legacy_and_explicit_supported_version_are_accepted(self):
        manager = ProviderManager()
        with patch(
            "projects.generation.providers.provider_manager.importlib.import_module",
            return_value=SimpleNamespace(LegacyProvider=LegacyProvider, VersionedProvider=VersionedProvider),
        ):
            manager.register("Legacy", "projects.generation.providers.legacy", "LegacyProvider")
            manager.register("Versioned", "projects.generation.providers.versioned", "VersionedProvider")
        self.assertEqual(sorted(manager.providers), ["Legacy", "Versioned"])

    def test_unsupported_and_malformed_versions_are_rejected(self):
        for provider_class in (UnsupportedProvider, MalformedVersionProvider):
            with self.subTest(provider_class=provider_class.__name__):
                manager = ProviderManager()
                with patch(
                    "projects.generation.providers.provider_manager.importlib.import_module",
                    return_value=SimpleNamespace(CandidateProvider=provider_class),
                ):
                    with self.assertRaisesRegex(ValueError, "registration failed"):
                        manager.register("Candidate", "projects.generation.providers.candidate", "CandidateProvider")
                self.assertEqual(manager.providers, {})

    def test_registry_activation_is_atomic_when_later_version_is_unsupported(self):
        manager = ProviderManager()
        existing = object()
        manager.providers = {"Existing": existing}
        modules = {
            "projects.generation.providers.good": SimpleNamespace(GoodProvider=VersionedProvider),
            "projects.generation.providers.bad": SimpleNamespace(BadProvider=UnsupportedProvider),
        }
        with patch(
            "projects.generation.providers.provider_manager.importlib.import_module",
            side_effect=lambda name: modules[name],
        ):
            with self.assertRaisesRegex(ValueError, "registry loading failed"):
                manager.load_registry({
                    "provider_modules": {
                        "Good": "projects/generation/providers/good.py",
                        "Bad": "projects/generation/providers/bad.py",
                    }
                })
        self.assertEqual(manager.providers, {"Existing": existing})


if __name__ == "__main__":
    unittest.main()
