import unittest

from projects.provider_store.execution_limiter import ProviderExecutionLimiter
from projects.provider_store.provider_service import ProviderService


class _ConnectionManager:
    def verify_connectivity(self, provider_id, probe, **kwargs):
        return {"status": "CONNECTED", "verified": True}


class ProviderServiceExecutionLimiterTests(unittest.TestCase):
    def test_operation_runs_under_slot_and_releases_it(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1)
        service = ProviderService(_ConnectionManager(), execution_limiter=limiter)
        result = service.execute_with_provider_slot(
            "image", "req-1", lambda: {"status": "COMPLETED", "output": "image.png"}
        )
        self.assertEqual(result["status"], "EXECUTED")
        self.assertEqual(result["result"]["status"], "COMPLETED")
        self.assertEqual(limiter.summary()["active"], 0)

    def test_operation_errors_are_sanitized_and_slot_is_released(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1)
        service = ProviderService(_ConnectionManager(), execution_limiter=limiter)
        secret = "Bearer never-echo-this"
        result = service.execute_with_provider_slot(
            "image", "req-2", lambda: (_ for _ in ()).throw(RuntimeError(secret))
        )
        self.assertEqual(result["status"], "PROVIDER_OPERATION_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn(secret, repr(result))
        self.assertEqual(limiter.summary()["active"], 0)

    def test_admission_rejection_prevents_provider_operation(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1)
        limiter.acquire("image", "occupied")
        service = ProviderService(_ConnectionManager(), execution_limiter=limiter)
        calls = []
        result = service.execute_with_provider_slot(
            "image", "blocked", lambda: calls.append(True) or {"status": "COMPLETED"}
        )
        self.assertEqual(result["status"], "OVERLOADED")
        self.assertEqual(calls, [])
        limiter.release("image", "occupied")

    def test_invalid_result_and_missing_limiter_fail_closed(self):
        service = ProviderService(
            _ConnectionManager(), execution_limiter=ProviderExecutionLimiter()
        )
        invalid = service.execute_with_provider_slot("image", "req-3", lambda: "not-a-dict")
        self.assertEqual(invalid["status"], "PROVIDER_OPERATION_INVALID_RESULT")
        missing = ProviderService(_ConnectionManager()).execute_with_provider_slot(
            "image", "req-4", lambda: {"status": "COMPLETED"}
        )
        self.assertEqual(missing["status"], "EXECUTION_LIMITER_UNAVAILABLE")
        bad_callable = service.execute_with_provider_slot("image", "req-5", None)
        self.assertEqual(bad_callable["status"], "INVALID_PROVIDER_OPERATION")

    def test_slot_release_failure_is_reported(self):
        class BrokenReleaseLimiter:
            def acquire(self, provider_id, request_id, **kwargs):
                return {"acquired": True, "status": "ACQUIRED"}
            def release(self, provider_id, request_id):
                return {"released": False, "status": "NOT_ACTIVE"}
        service = ProviderService(_ConnectionManager(), execution_limiter=BrokenReleaseLimiter())
        result = service.execute_with_provider_slot(
            "image", "req-6", lambda: {"status": "COMPLETED"}
        )
        self.assertEqual(result["status"], "EXECUTION_SLOT_RELEASE_FAILED")


if __name__ == "__main__":
    unittest.main()
