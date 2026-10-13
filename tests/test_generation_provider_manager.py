import unittest

from projects.generation.providers.provider_manager import ProviderManager


class GenerationProviderManagerSecurityTests(unittest.TestCase):
    def test_builtin_provider_loads_from_explicit_allowlist(self):
        manager = ProviderManager()
        manager.load_registry({
            "provider_modules": {
                "Test Provider": "projects/generation/providers/test_provider.py",
            }
        })
        provider = manager.get("Test Provider")
        self.assertIsNotNone(provider)
        self.assertTrue(callable(provider.generate_image))

    def test_registry_cannot_select_arbitrary_module_or_class(self):
        manager = ProviderManager()
        with self.assertRaisesRegex(ValueError, "does not match the built-in allowlist"):
            manager.load_registry({
                "provider_modules": {
                    "Test Provider": "os.py",
                }
            })
        with self.assertRaisesRegex(ValueError, "outside the built-in allowlist"):
            manager.load_registry({
                "provider_modules": {
                    "Untrusted": "projects/generation/providers/test_provider.py",
                }
            })
        with self.assertRaisesRegex(ValueError, "module does not match"):
            manager.register(
                "Test Provider", "os", "system"
            )

    def test_invalid_later_registry_entry_does_not_partially_load_earlier_provider(self):
        manager = ProviderManager()
        manager.providers["existing"] = object()
        with self.assertRaises(ValueError):
            manager.load_registry({
                "provider_modules": {
                    "Test Provider": "projects/generation/providers/test_provider.py",
                    "Untrusted": "projects/generation/providers/test_provider.py",
                }
            })
        self.assertEqual(set(manager.providers), {"existing"})

    def test_invalid_registry_shape_is_rejected(self):
        manager = ProviderManager()
        with self.assertRaises(ValueError):
            manager.load_registry([])
        with self.assertRaisesRegex(ValueError, "provider_modules"):
            manager.load_registry({"provider_modules": []})


if __name__ == "__main__":
    unittest.main()
