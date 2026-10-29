import unittest
from types import SimpleNamespace
from unittest.mock import patch

from projects.generation.providers.provider_manager import ProviderManager


class ProviderManagerSecurityTests(unittest.TestCase):
    def test_registry_loads_only_explicit_provider_package_modules(self):
        class HuggingFaceProvider:
            def generate_image(self, *args, **kwargs):
                return {"success": True}
        class PollinationsProvider:
            def generate_image(self, *args, **kwargs):
                return {"success": True}

        def importer(module_name):
            modules = {
                "projects.generation.providers.huggingface_provider": SimpleNamespace(
                    HuggingFaceProvider=HuggingFaceProvider
                ),
                "projects.generation.providers.pollinations_provider": SimpleNamespace(
                    PollinationsProvider=PollinationsProvider
                ),
            }
            return modules[module_name]

        manager = ProviderManager()
        manager.providers["Removed Provider"] = object()
        with patch("projects.generation.providers.provider_manager.importlib.import_module", side_effect=importer):
            result = manager.load_registry({
                "provider_modules": {
                    "Hugging Face": "projects/generation/providers/huggingface_provider.py",
                    "Pollinations": "projects/generation/providers/pollinations_provider.py",
                }
            })

        self.assertEqual(result, {"loaded": ["Hugging Face", "Pollinations"], "count": 2})
        self.assertIsInstance(manager.get("Hugging Face"), HuggingFaceProvider)
        self.assertIsInstance(manager.get("Pollinations"), PollinationsProvider)
        self.assertIsNone(manager.get("Removed Provider"))

    def test_registry_rejects_path_traversal_absolute_and_untrusted_modules_before_import(self):
        bad_paths = (
            "../evil.py",
            "/tmp/evil.py",
            "projects/generation/providers/../../evil.py",
            r"projects\generation\providers\evil.py",
            "projects/generation/evil.py",
            "projects/generation/providers/evil.txt",
            "projects/generation/providers/evil-module.py",
        )
        for path in bad_paths:
            with self.subTest(path=path):
                manager = ProviderManager()
                with patch("projects.generation.providers.provider_manager.importlib.import_module") as importer:
                    with self.assertRaises(ValueError):
                        manager.load_registry({"provider_modules": {"Hugging Face": path}})
                    importer.assert_not_called()

    def test_registry_rejects_invalid_names_and_duplicate_modules(self):
        manager = ProviderManager()
        for registry in (
            {"provider_modules": {"../Injected": "projects/generation/providers/fake_provider.py"}},
            {"provider_modules": {"Hugging Face": "projects/generation/providers/fake_provider.py",
                                  "Pollinations": "projects/generation/providers/fake_provider.py"}},
            {"provider_modules": []},
            [],
        ):
            with self.subTest(registry=registry):
                with patch("projects.generation.providers.provider_manager.importlib.import_module") as importer:
                    with self.assertRaises(ValueError):
                        manager.load_registry(registry)
                    importer.assert_not_called()

    def test_failed_registry_load_preserves_existing_provider_set(self):
        sentinel = object()
        manager = ProviderManager()
        manager.providers["Existing"] = sentinel

        def importer(module_name):
            if module_name.endswith("first_provider"):
                return SimpleNamespace(FirstProvider=lambda: SimpleNamespace(generate_image=lambda *args, **kwargs: {"success": True}))
            raise ImportError("private import detail")

        with patch("projects.generation.providers.provider_manager.importlib.import_module", side_effect=importer):
            with self.assertRaisesRegex(ValueError, "provider registry loading failed"):
                manager.load_registry({
                    "provider_modules": {
                        "First": "projects/generation/providers/first_provider.py",
                        "Second": "projects/generation/providers/second_provider.py",
                    }
                })

        self.assertEqual(manager.providers, {"Existing": sentinel})

    def test_registry_rejects_provider_without_required_generation_method(self):
        class IncompleteProvider:
            pass
        manager = ProviderManager()
        manager.providers["Existing"] = object()
        with patch(
            "projects.generation.providers.provider_manager.importlib.import_module",
            return_value=SimpleNamespace(IncompleteProvider=IncompleteProvider),
        ):
            with self.assertRaisesRegex(ValueError, "provider registry loading failed"):
                manager.load_registry({
                    "provider_modules": {
                        "Incomplete": "projects/generation/providers/incomplete_provider.py",
                    }
                })
        self.assertEqual(list(manager.providers), ["Existing"])

    def test_direct_registration_rejects_arbitrary_imports(self):
        manager = ProviderManager()
        with patch("projects.generation.providers.provider_manager.importlib.import_module") as importer:
            with self.assertRaises(ValueError):
                manager.register("Injected", "os", "system")
            importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
