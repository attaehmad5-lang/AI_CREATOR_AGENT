import unittest

from projects.generation.providers.provider_manager import ProviderManager
from projects.generation.providers.fallback_manager import FallbackManager
from projects.generation.providers.provider_health import ProviderHealth
from projects.provider_store.execution_limiter import ProviderExecutionLimiter


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

    def test_paid_provider_never_executes_directly_without_policy_handler(self):
        manager = ProviderManager()
        class PaidProvider:
            requires_paid_approval = True
            called = False
            def generate_image(self, prompt, output_path):
                self.called = True
                return {"status": "COMPLETED"}
        provider = PaidProvider()
        manager.providers["paid"] = provider
        result = FallbackManager(manager).generate_image(["paid"], "prompt", "out.png")
        self.assertEqual(result["status"], "PAID_EXECUTION_HANDLER_REQUIRED")
        self.assertFalse(provider.called)
        self.assertEqual(result["attempted_providers"][0]["status"], "PAID_EXECUTION_HANDLER_REQUIRED")

    def test_missing_paid_handler_does_not_send_prompt_to_fallback_provider(self):
        manager = ProviderManager()
        calls = []
        class PaidProvider:
            requires_paid_approval = True
            def generate_image(self, prompt, output_path):
                raise AssertionError("paid provider must not execute directly")
        class AlternateProvider:
            def generate_image(self, prompt, output_path):
                calls.append(prompt)
                return {"status": "COMPLETED"}
        manager.providers.update({"paid": PaidProvider(), "alternate": AlternateProvider()})
        result = FallbackManager(manager).generate_image(
            ["paid", "alternate"], "sensitive documentary prompt", "out.png"
        )
        self.assertEqual(result["status"], "PAID_EXECUTION_HANDLER_REQUIRED")
        self.assertEqual(result["provider"], "paid")
        self.assertEqual(calls, [])

    def test_paid_handler_failure_does_not_silently_try_another_provider(self):
        manager = ProviderManager()
        calls = []
        class PaidProvider:
            requires_paid_approval = True
            def generate_image(self, prompt, output_path):
                raise AssertionError("paid provider must be handled through policy handler")
        class FreeProvider:
            def generate_image(self, prompt, output_path):
                calls.append("free")
                return {"status": "COMPLETED"}
        manager.providers.update({"paid": PaidProvider(), "free": FreeProvider()})
        result = FallbackManager(
            manager,
            paid_execution_handler=lambda **kwargs: {"status": "PROJECT_BUDGET_EXCEEDED"},
        ).generate_image(["paid", "free"], "prompt", "out.png")
        self.assertEqual(result["status"], "PROJECT_BUDGET_EXCEEDED")
        self.assertEqual(calls, [])

    def test_paid_handler_status_is_sanitized_and_invalid_results_fail_closed(self):
        manager = ProviderManager()
        class PaidProvider:
            requires_paid_approval = True
        manager.providers["paid"] = PaidProvider()
        unsafe = FallbackManager(
            manager,
            paid_execution_handler=lambda **kwargs: {"status": "failure\nBearer secret"},
        ).generate_image(["paid"], "prompt", "out.png")
        self.assertEqual(unsafe["status"], "PAID_EXECUTION_FAILED")
        invalid = FallbackManager(
            manager,
            paid_execution_handler=lambda **kwargs: ["COMPLETED"],
        ).generate_image(["paid"], "prompt", "out.png")
        self.assertEqual(invalid["status"], "PAID_EXECUTION_HANDLER_INVALID_RESULT")

    def test_paid_handler_completed_result_is_returned_with_attempt_history(self):
        manager = ProviderManager()
        class PaidProvider:
            requires_paid_approval = True
        manager.providers["paid"] = PaidProvider()
        result = FallbackManager(
            manager,
            paid_execution_handler=lambda **kwargs: {
                "status": "COMPLETED", "output_path": "out.png",
            },
        ).generate_image(["missing", "paid"], "prompt", "out.png")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["attempted_providers"][0]["provider"], "missing")
        self.assertEqual(result["attempted_providers"][-1]["status"], "COMPLETED")

    def test_fallback_manager_enforces_provider_slots_and_releases_them(self):
        manager = ProviderManager()
        class ImageProvider:
            provider_id = "image-provider"
            def __init__(self):
                self.calls = 0
            def generate_image(self, prompt, output_path):
                self.calls += 1
                return {"status": "COMPLETED", "output_path": output_path}
        provider = ImageProvider()
        manager.providers["image"] = provider
        limiter = ProviderExecutionLimiter(max_concurrent=1, max_pending=0)
        limiter.acquire("image-provider", "occupied")
        fallback = FallbackManager(manager, execution_limiter=limiter)
        blocked = fallback.generate_image(
            ["image"], "prompt", "out.png", request_id="blocked"
        )
        self.assertEqual(blocked["status"], "OVERLOADED")
        self.assertEqual(provider.calls, 0)
        limiter.release("image-provider", "occupied")

        completed = fallback.generate_image(
            ["image"], "prompt", "out.png", request_id="allowed"
        )
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(limiter.summary()["active"], 0)

    def test_limited_fallback_requires_request_id_and_does_not_execute(self):
        manager = ProviderManager()
        calls = []
        class ImageProvider:
            provider_id = "image-provider"
            def generate_image(self, prompt, output_path):
                calls.append(True)
                return {"status": "COMPLETED"}
        manager.providers["image"] = ImageProvider()
        fallback = FallbackManager(
            manager,
            execution_limiter=ProviderExecutionLimiter(max_concurrent=1),
        )
        result = fallback.generate_image(["image"], "prompt", "out.png")
        self.assertEqual(result["status"], "REQUEST_ID_REQUIRED")
        self.assertEqual(calls, [])

    def test_paid_handler_runs_under_limiter_and_releases_slot(self):
        manager = ProviderManager()
        class PaidProvider:
            provider_id = "paid-provider"
            requires_paid_approval = True
        manager.providers["paid"] = PaidProvider()
        limiter = ProviderExecutionLimiter(max_concurrent=1)
        fallback = FallbackManager(
            manager,
            execution_limiter=limiter,
            paid_execution_handler=lambda **kwargs: {"status": "COMPLETED", "provider": "paid"},
        )
        result = fallback.generate_image(
            ["paid"], "prompt", "out.png", request_id="paid-request"
        )
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(limiter.summary()["active"], 0)

    def test_invalid_registry_shape_is_rejected(self):
        manager = ProviderManager()
        with self.assertRaises(ValueError):
            manager.load_registry([])
        with self.assertRaisesRegex(ValueError, "provider_modules"):
            manager.load_registry({"provider_modules": []})


if __name__ == "__main__":
    unittest.main()
