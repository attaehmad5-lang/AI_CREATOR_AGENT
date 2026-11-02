import unittest

from projects.provider_store.provider_service import ProviderService


class ConnectionStub:
    def __init__(self, provider=None, fail=False):
        self.provider = provider or {"id": "image", "enabled": True}
        self.fail = fail

    def verify_connectivity(self, *args, **kwargs):
        return {"verified": False}

    def get_provider(self, provider_id):
        if self.fail:
            raise OSError("private provider-store detail")
        if provider_id != "image":
            return None
        return self.provider


class HealthStub:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "provider": "image", "status": "HEALTHY",
            "verified": True, "latency_ms": 12.345,
        }
        self.error = error
        self.calls = []

    def record_result(self, provider_id, **kwargs):
        return {"saved": True}

    def active_probe(self, provider_id, probe, *, timeout_seconds=5.0):
        self.calls.append((provider_id, probe, timeout_seconds))
        if self.error:
            raise self.error
        return self.result


class AuthorizationStub:
    def __init__(self, allowed):
        self.allowed = allowed
        self.calls = []

    def authorize(self, principal_id, operation):
        self.calls.append((principal_id, operation))
        return {"allowed": self.allowed, "status": "ALLOWED" if self.allowed else "ACCESS_DENIED"}


class ProviderHealthProbeFacadeTests(unittest.TestCase):
    def test_success_returns_only_verified_safe_health_evidence(self):
        health = HealthStub()
        service = ProviderService(ConnectionStub(), health_manager=health)
        probe = lambda timeout_seconds: {"ok": True, "secret": "never-return"}
        result = service.active_probe_provider("image", probe, timeout_seconds=2.5)
        self.assertEqual(result, {
            "status": "HEALTHY", "provider": "image", "verified": True,
            "saved": True, "latency_ms": 12.35,
        })
        self.assertEqual(health.calls[0], ("image", probe, 2.5))
        self.assertNotIn("never-return", repr(result))

    def test_failed_probe_is_recorded_but_never_reported_verified(self):
        health = HealthStub({
            "provider": "image", "status": "TIMEOUT",
            "verified": False, "error_type": "TimeoutError",
            "private_detail": "do-not-return",
        })
        service = ProviderService(ConnectionStub(), health_manager=health)
        result = service.active_probe_provider("image", lambda **kwargs: False)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertFalse(result["verified"])
        self.assertTrue(result["saved"])
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertNotIn("private_detail", repr(result))

    def test_disabled_and_unknown_providers_are_not_probed(self):
        health = HealthStub()
        disabled = ProviderService(
            ConnectionStub({"id": "image", "enabled": False}),
            health_manager=health,
        )
        self.assertEqual(
            disabled.active_probe_provider("image", lambda **kwargs: True)["status"],
            "PROVIDER_DISABLED",
        )
        missing = ProviderService(ConnectionStub(), health_manager=health)
        self.assertEqual(
            missing.active_probe_provider("missing", lambda **kwargs: True)["status"],
            "NOT_FOUND",
        )
        self.assertEqual(health.calls, [])

    def test_authorization_and_invalid_timeouts_block_probe(self):
        health = HealthStub()
        auth = AuthorizationStub(False)
        service = ProviderService(
            ConnectionStub(), health_manager=health, authorization_manager=auth,
        )
        denied = service.active_probe_provider(
            "image", lambda **kwargs: True, principal_id="viewer",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertFalse(denied["saved"])
        invalid_timeout_service = ProviderService(
            ConnectionStub(), health_manager=health,
        )
        for invalid in (0, -1, 31, float("nan"), True):
            with self.subTest(timeout=invalid):
                result = invalid_timeout_service.active_probe_provider(
                    "image", lambda **kwargs: True, timeout_seconds=invalid,
                )
                self.assertEqual(result["status"], "INVALID_TIMEOUT")
        self.assertEqual(health.calls, [])

    def test_malformed_probe_status_and_inconsistent_health_flags_fail_closed(self):
        malformed_results = (
            {"status": [], "verified": False},
            {"status": {}, "verified": False},
            {"status": "HEALTHY", "verified": "yes"},
            {"status": "TIMEOUT", "verified": True},
        )
        for malformed in malformed_results:
            with self.subTest(result=malformed):
                health = HealthStub(malformed)
                service = ProviderService(ConnectionStub(), health_manager=health)
                result = service.active_probe_provider(
                    "image", lambda **kwargs: True,
                )
                self.assertEqual(result["status"], "INVALID_ACTIVE_PROBE_RESPONSE")
                self.assertFalse(result["verified"])
                self.assertFalse(result["saved"])

    def test_missing_health_manager_and_probe_exception_fail_closed(self):
        service = ProviderService(ConnectionStub())
        self.assertEqual(
            service.active_probe_provider("image", lambda **kwargs: True)["status"],
            "HEALTH_MANAGER_UNAVAILABLE",
        )
        health = HealthStub(error=RuntimeError("credential and endpoint secret"))
        service = ProviderService(ConnectionStub(), health_manager=health)
        result = service.active_probe_provider("image", lambda **kwargs: True)
        self.assertEqual(result["status"], "ACTIVE_PROBE_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("credential", repr(result))
        self.assertNotIn("endpoint secret", repr(result))


if __name__ == "__main__":
    unittest.main()
