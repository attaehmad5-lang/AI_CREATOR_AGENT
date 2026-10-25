import unittest

from projects.provider_store.provider_service import ProviderService


class LocalTextServiceIntegrationTests(unittest.TestCase):
    class Connections:
        def verify_connectivity(self, *args, **kwargs):
            return {"verified": True}

    class Auth:
        def __init__(self, allowed):
            self.allowed = allowed
            self.calls = []

        def authorize(self, principal_id, operation):
            self.calls.append((principal_id, operation))
            return {"allowed": self.allowed}

    class Flags:
        def __init__(self, enabled):
            self.enabled = enabled

        def evaluate(self, name, **kwargs):
            return {"enabled": self.enabled}

    class Privacy:
        def __init__(self, allowed):
            self.allowed = allowed
            self.seen = None

        def inspect(self, payload):
            self.seen = payload
            return {"allowed": self.allowed}

    class Adapter:
        def __init__(self):
            self.calls = []

        def generate_text(self, prompt, *, system=None):
            self.calls.append((prompt, system))
            return {"status": "LOCAL_TEXT_GENERATED", "generated": True, "text": "local answer"}

    def service(self, **kwargs):
        return ProviderService(self.Connections(), **kwargs)

    def test_authorization_denial_stops_before_local_adapter(self):
        auth, adapter = self.Auth(False), self.Adapter()
        result = self.service(authorization_manager=auth, local_model_adapter=adapter).generate_local_text(
            prompt="test prompt", principal_id="user-a"
        )
        self.assertEqual(result["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(auth.calls, [("user-a", "provider.generate")])
        self.assertEqual(adapter.calls, [])

    def test_feature_flag_and_sensitive_data_policy_fail_closed(self):
        adapter = self.Adapter()
        disabled = self.service(
            authorization_manager=self.Auth(True), feature_flag_manager=self.Flags(False),
            local_model_adapter=adapter,
        ).generate_local_text(prompt="test prompt", principal_id="user-a")
        self.assertEqual(disabled["status"], "FEATURE_DISABLED")
        self.assertEqual(adapter.calls, [])

        privacy = self.Privacy(False)
        blocked = self.service(
            authorization_manager=self.Auth(True), feature_flag_manager=self.Flags(True),
            sensitive_data_policy=privacy, local_model_adapter=adapter,
        ).generate_local_text(prompt="test prompt", system="system rules", principal_id="user-a")
        self.assertEqual(blocked["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertEqual(privacy.seen, {"prompt": "test prompt", "system": "system rules"})
        self.assertEqual(adapter.calls, [])

    def test_allowed_request_reaches_local_adapter_only_after_gates(self):
        auth, privacy, adapter = self.Auth(True), self.Privacy(True), self.Adapter()
        result = self.service(
            authorization_manager=auth, feature_flag_manager=self.Flags(True),
            sensitive_data_policy=privacy, local_model_adapter=adapter,
        ).generate_local_text(
            prompt="draft a scene", system="be accurate", principal_id="user-a",
            project_id="film", request_id="local-1",
        )
        self.assertEqual(result["status"], "LOCAL_TEXT_GENERATED")
        self.assertEqual(adapter.calls, [("draft a scene", "be accurate")])
        self.assertEqual(auth.calls, [("user-a", "provider.generate")])

    def test_missing_or_malformed_adapter_response_fails_closed(self):
        result = self.service().generate_local_text(prompt="hello")
        self.assertEqual(result["status"], "LOCAL_MODEL_ADAPTER_UNAVAILABLE")

        class InvalidAdapter:
            def generate_text(self, prompt, *, system=None):
                return ["invalid"]
        invalid = self.service(local_model_adapter=InvalidAdapter()).generate_local_text(prompt="hello")
        self.assertEqual(invalid["status"], "INVALID_LOCAL_MODEL_RESPONSE")
        self.assertFalse(invalid["generated"])


if __name__ == "__main__":
    unittest.main()
