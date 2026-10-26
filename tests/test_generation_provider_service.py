import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from projects.generation.providers.provider_service import DEFAULT_REGISTRY_PATH, ProviderService


class GenerationProviderServiceTests(unittest.TestCase):
    def test_default_registry_path_is_independent_of_current_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("projects.generation.providers.provider_service.ProviderManager") as manager_cls, \
                 patch("projects.generation.providers.provider_service.FallbackManager"), \
                 patch("projects.generation.providers.provider_service.ProviderHealth"):
                original = Path.cwd()
                try:
                    os.chdir(directory)
                    catalog = SimpleNamespace(
                        list_providers=lambda: [
                            {"id": "huggingface", "name": "Hugging Face", "category": "image", "enabled": True},
                            {"id": "pollinations", "name": "Pollinations", "category": "image", "enabled": True},
                        ],
                        validate_provider_contract=lambda provider_id, instance: {"valid": True},
                    )
                    service = ProviderService(connection_manager=catalog)
                finally:
                    os.chdir(original)

        self.assertEqual(DEFAULT_REGISTRY_PATH, Path(__file__).resolve().parents[1] / "projects" / "generation" / "provider_registry.json")
        manager_cls.return_value.load_registry.assert_called_once_with(service.registry["image"])

    def test_invalid_registry_root_and_image_section_fail_closed(self):
        for payload in ([], {"image": []}, {"video": {}}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "registry.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with patch("projects.generation.providers.provider_service.ProviderManager") as manager_cls:
                    with self.assertRaises(ValueError):
                        ProviderService(path)
                    manager_cls.assert_not_called()

    def test_disabled_central_catalog_provider_is_rejected_before_runtime_loading(self):
        payload = {
            "image": {
                "primary": "Hugging Face", "fallbacks": [],
                "provider_modules": {
                    "Hugging Face": "projects/generation/providers/huggingface_provider.py",
                },
            }
        }
        class Catalog:
            def list_providers(self):
                return [{"id": "huggingface", "name": "Hugging Face", "category": "image", "enabled": False}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("projects.generation.providers.provider_service.ProviderManager") as manager_cls:
                with self.assertRaisesRegex(ValueError, "missing, disabled, or incompatible"):
                    ProviderService(path, connection_manager=Catalog())
                manager_cls.assert_not_called()

    def test_runtime_adapter_must_pass_central_catalog_contract(self):
        payload = {
            "image": {
                "primary": "Hugging Face", "fallbacks": [],
                "provider_modules": {
                    "Hugging Face": "projects/generation/providers/huggingface_provider.py",
                },
            }
        }
        class Catalog:
            def list_providers(self):
                return [{"id": "huggingface", "name": "Hugging Face", "category": "image", "enabled": True}]
            def validate_provider_contract(self, provider_id, instance):
                self.args = (provider_id, instance)
                return {"valid": False}
        catalog = Catalog()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("projects.generation.providers.provider_service.ProviderManager") as manager_cls:
                with self.assertRaisesRegex(ValueError, "runtime provider contract validation failed"):
                    ProviderService(path, connection_manager=catalog)
                manager_cls.return_value.load_registry.assert_called_once_with(payload["image"])
                self.assertEqual(catalog.args[0], "huggingface")

    def test_custom_relative_registry_path_keeps_explicit_caller_semantics(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                path = Path("registry.json")
                path.write_text(json.dumps({"image": {"primary": None, "fallbacks": [], "provider_modules": {}}}), encoding="utf-8")
                with patch("projects.generation.providers.provider_service.ProviderManager") as manager_cls, \
                     patch("projects.generation.providers.provider_service.FallbackManager"), \
                     patch("projects.generation.providers.provider_service.ProviderHealth"):
                    catalog = SimpleNamespace(list_providers=lambda: [], validate_provider_contract=lambda *args: {"valid": True})
                    service = ProviderService(path, connection_manager=catalog)
            finally:
                os.chdir(original)
        self.assertEqual(service.registry["image"]["fallbacks"], [])
        manager_cls.return_value.load_registry.assert_called_once_with(service.registry["image"])


if __name__ == "__main__":
    unittest.main()
