import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_onboarding_manager import ProviderOnboardingManager


class ProviderOnboardingManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "onboarding.json"
        self.registered = []
        self.enabled = []
        self.authorized = []
        self.manager = ProviderOnboardingManager(
            self.path,
            register_disabled=lambda **kwargs: self.registered.append(kwargs) or {"saved": True, "enabled": False},
            reviewer_authorizer=lambda **kwargs: self.authorized.append(kwargs) or True,
            probe_provider=lambda **kwargs: {"ok": True, "provider_id": kwargs["provider"]["id"]},
            enable_provider=lambda **kwargs: self.enabled.append(kwargs) or {"enabled": True},
        )
        self.provider = {
            "id": "provider-image", "category": "image",
            "docs_url": "https://docs.example.com/image-api",
            "capabilities": ["image_generation"],
            "allowed_hosts": ["api.example.com"],
            "adapter_api_version": "1.0",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_onboarding_requires_review_then_live_probe_then_authorized_enable(self):
        submitted = self.manager.submit(request_id="onboard-1", provider=self.provider)
        self.assertEqual(submitted["state"], "PENDING_REVIEW")
        self.assertEqual(self.registered[0]["enabled"], False)
        blocked = self.manager.verify(request_id="onboard-1")
        self.assertEqual(blocked["status"], "ONBOARDING_APPROVAL_REQUIRED")
        approved = self.manager.approve(request_id="onboard-1", reviewer_id="reviewer-1")
        self.assertTrue(approved["approved"])
        verified = self.manager.verify(request_id="onboard-1")
        self.assertTrue(verified["verified"])
        enabled = self.manager.enable(request_id="onboard-1", actor_id="admin-1")
        self.assertTrue(enabled["enabled"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["requests"]["onboard-1"]["state"], "ENABLED")
        self.assertEqual([event["action"] for event in stored["events"]], ["SUBMIT", "APPROVE", "VERIFY", "ENABLE"])

    def test_invalid_metadata_and_secret_keys_are_rejected_before_registration(self):
        cases = [
            {**self.provider, "docs_url": "http://docs.example.com"},
            {**self.provider, "allowed_hosts": []},
            {**self.provider, "capabilities": ["image_generation", "image_generation"]},
            {**self.provider, "api_key": "never-persist"},
            {**self.provider, "adapter_api_version": "not-a-version"},
        ]
        for index, provider in enumerate(cases):
            with self.subTest(index=index):
                result = self.manager.submit(request_id=f"invalid-{index}", provider=provider)
                self.assertEqual(result["status"], "INVALID_PROVIDER_ONBOARDING_METADATA")
        self.assertEqual(self.registered, [])

    def test_reviewer_and_enable_authorization_fail_closed(self):
        denied = ProviderOnboardingManager(
            self.path.with_name("denied.json"),
            register_disabled=lambda **kwargs: {"saved": True, "enabled": False},
            reviewer_authorizer=lambda **kwargs: False,
            probe_provider=lambda **kwargs: {"ok": True, "provider_id": kwargs["provider"]["id"]},
            enable_provider=lambda **kwargs: {"enabled": True},
        )
        denied.submit(request_id="deny-1", provider=self.provider)
        self.assertEqual(denied.approve(request_id="deny-1", reviewer_id="reviewer-1")["status"], "REVIEW_FORBIDDEN")
        self.assertEqual(denied.enable(request_id="deny-1", actor_id="admin-1")["status"], "ENABLE_FORBIDDEN")

    def test_probe_failure_does_not_allow_enablement(self):
        manager = ProviderOnboardingManager(
            self.path.with_name("probe-fail.json"),
            register_disabled=lambda **kwargs: {"saved": True, "enabled": False},
            reviewer_authorizer=lambda **kwargs: True,
            probe_provider=lambda **kwargs: {"ok": True, "provider_id": "wrong-provider"},
            enable_provider=lambda **kwargs: {"enabled": True},
        )
        manager.submit(request_id="probe-1", provider=self.provider)
        manager.approve(request_id="probe-1", reviewer_id="reviewer-1")
        self.assertEqual(manager.verify(request_id="probe-1")["status"], "PROVIDER_PROBE_UNVERIFIED")
        self.assertEqual(manager.enable(request_id="probe-1", actor_id="admin-1")["status"], "LIVE_VERIFICATION_REQUIRED")

    def test_failed_registration_never_creates_approved_request(self):
        manager = ProviderOnboardingManager(
            self.path.with_name("registration-fail.json"),
            register_disabled=lambda **kwargs: {"saved": False, "enabled": False},
            reviewer_authorizer=lambda **kwargs: True,
            probe_provider=lambda **kwargs: {"ok": True, "provider_id": kwargs["provider"]["id"]},
            enable_provider=lambda **kwargs: {"enabled": True},
        )
        self.assertEqual(manager.submit(request_id="register-fail", provider=self.provider)["status"], "PROVIDER_REGISTRATION_FAILED")
        self.assertEqual(json.loads(manager.path.read_text(encoding="utf-8"))["requests"], {})

    def test_duplicate_request_and_invalid_timeout_fail_closed(self):
        self.manager.submit(request_id="duplicate", provider=self.provider)
        self.assertEqual(self.manager.submit(request_id="duplicate", provider=self.provider)["status"], "ONBOARDING_REQUEST_EXISTS")
        self.manager.approve(request_id="duplicate", reviewer_id="reviewer-1")
        self.assertEqual(self.manager.verify(request_id="duplicate", timeout_seconds=0)["status"], "INVALID_TIMEOUT")

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderOnboardingManager(
                self.path,
                register_disabled=lambda **kwargs: {"saved": True, "enabled": False},
                reviewer_authorizer=lambda **kwargs: True,
                probe_provider=lambda **kwargs: {"ok": True, "provider_id": "provider-image"},
                enable_provider=lambda **kwargs: {"enabled": True},
            )


if __name__ == "__main__":
    unittest.main()
