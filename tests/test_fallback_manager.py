import unittest

from projects.generation.providers.fallback_manager import FallbackManager
from projects.generation.providers.pollinations_provider import PollinationsProviderError


class ProviderStub:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def generate_image(self, prompt, output_path):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class ManagerStub:
    def __init__(self, providers):
        self.providers = providers

    def get(self, name):
        return self.providers.get(name)


class FallbackManagerTests(unittest.TestCase):
    def test_402_billing_failure_stops_before_fallback_provider(self):
        primary = ProviderStub(error=PollinationsProviderError(
            "budget exhausted", status_code=402, retryable=False
        ))
        fallback = ProviderStub(result={"status": "COMPLETED"})
        manager = FallbackManager(ManagerStub({"primary": primary, "fallback": fallback}))
        result = manager.generate_image(["primary", "fallback"], "prompt", "out.jpg")
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["fallback_stopped"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_retryable_failure_continues_to_fallback(self):
        primary = ProviderStub(error=PollinationsProviderError(
            "temporary unavailable", status_code=503, retryable=True
        ))
        fallback = ProviderStub(result={"status": "COMPLETED", "provider": "fallback"})
        manager = FallbackManager(ManagerStub({"primary": primary, "fallback": fallback}))
        result = manager.generate_image(["primary", "fallback"], "prompt", "out.jpg")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fallback.calls, 1)

    def test_legacy_exceptions_remain_fallback_compatible(self):
        primary = ProviderStub(error=RuntimeError("legacy provider error"))
        fallback = ProviderStub(result={"status": "COMPLETED"})
        manager = FallbackManager(ManagerStub({"primary": primary, "fallback": fallback}))
        result = manager.generate_image(["primary", "fallback"], "prompt", "out.jpg")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(fallback.calls, 1)


if __name__ == "__main__":
    unittest.main()
