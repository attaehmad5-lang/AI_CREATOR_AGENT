"""Edge-case and shipped-adapter contract tests for generation provider registration."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from projects.generation.providers.provider_manager import ProviderManager


class RaisingVersionProvider:
    @property
    def PROVIDER_INTERFACE_VERSION(self):
        raise RuntimeError("private provider details")

    def generate_image(self, prompt, output_path):
        return None


class FloatVersionProvider:
    PROVIDER_INTERFACE_VERSION = 1.0

    def generate_image(self, prompt, output_path):
        return None


class MissingMethodProvider:
    PROVIDER_INTERFACE_VERSION = "1.0"


class ProviderInterfaceEdgeCaseTests(unittest.TestCase):
    def test_unreadable_non_string_and_method_less_adapters_fail_closed(self):
        for provider_class in (RaisingVersionProvider, FloatVersionProvider, MissingMethodProvider):
            with self.subTest(provider_class=provider_class.__name__):
                manager = ProviderManager()
                with patch(
                    "projects.generation.providers.provider_manager.importlib.import_module",
                    return_value=SimpleNamespace(CandidateProvider=provider_class),
                ):
                    with self.assertRaises(ValueError) as caught:
                        manager.register(
                            "Candidate", "projects.generation.providers.candidate", "CandidateProvider"
                        )
                self.assertNotIn("private provider details", repr(caught.exception.__cause__))
                self.assertEqual(manager.providers, {})

    def test_unsafe_module_paths_are_rejected(self):
        for value in ("../outside.py", "/tmp/provider.py", "projects/generation/providers/../outside.py", "projects/other/provider.py"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderManager._module_from_path(value)


class ShippedAdapterContractTests(unittest.TestCase):
    """Dependency-free static contract check for the adapters shipped in the repository."""

    def test_every_shipped_image_adapter_meets_the_current_contract(self):
        root = Path(__file__).resolve().parents[1]
        registry = json.loads(
            (root / "projects" / "generation" / "provider_registry.json").read_text(encoding="utf-8")
        )
        modules = registry["image"]["provider_modules"]
        self.assertTrue(modules)
        for name, module_path in modules.items():
            with self.subTest(provider=name):
                ProviderManager._module_from_path(module_path)
                tree = ast.parse((root / module_path).read_text(encoding="utf-8"))
                expected_class = name.replace(" ", "") + "Provider"
                classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == expected_class]
                self.assertEqual(len(classes), 1)
                methods = {
                    node.name for node in classes[0].body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertIn("generate_image", methods)
                declared = [
                    ast.literal_eval(node.value)
                    for node in classes[0].body
                    if isinstance(node, ast.Assign)
                    and any(getattr(target, "id", None) == "PROVIDER_INTERFACE_VERSION" for target in node.targets)
                ]
                self.assertTrue(all(version == "1.0" for version in declared))


if __name__ == "__main__":
    unittest.main()
