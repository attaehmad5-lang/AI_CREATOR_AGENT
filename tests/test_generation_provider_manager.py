import unittest

from projects.generation.providers.provider_manager import ProviderManager
from projects.generation.providers.fallback_manager import FallbackManager
from projects.generation.providers.provider_health import ProviderHealth


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

    def test_fallback_results_and_health_state_never_store_raw_exception_text(self):
        manager = ProviderManager()
        secret = "Bearer very-secret-token"
        class FailingProvider:
            def generate_image(self, prompt, output_path):
                raise RuntimeError(f"request failed with {secret}")
        manager.providers["failing"] = FailingProvider()
        health = ProviderHealth()
        result = FallbackManager(manager, health).generate_image(
            ["failing"], "test prompt", "output.png"
        )
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result["errors"][0]["error_type"], "RuntimeError")
        status = health.get_status("failing")
        self.assertEqual(status["last_error"], "RuntimeError")
        self.assertNotIn(secret, repr(status))

    def test_invalid_registry_shape_is_rejected(self):
        manager = ProviderManager()
        with self.assertRaises(ValueError):
            manager.load_registry([])
        with self.assertRaisesRegex(ValueError, "provider_modules"):
            manager.load_registry({"provider_modules": []})


if __name__ == "__main__":
    unittest.main()
