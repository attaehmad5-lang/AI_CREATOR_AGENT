"""Tests for provider onboarding metadata validation and disabled-by-default registration."""
from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager

from projects.provider_store.provider_onboarding import ProviderOnboardingWorkflow


class FakeConnectionManager:
    def __init__(self):
        self.catalog = {"version": "1.0", "providers": []}
        self.registered = []

    @staticmethod
    def _contains_credential_value_field(provider):
        forbidden = {"api_key", "api_key_value", "token", "password", "secret"}
        return any(key.casefold() in forbidden for key in provider)

    @staticmethod
    def validate_catalog_schema(catalog):
        return {"valid": isinstance(catalog, dict) and isinstance(catalog.get("providers"), list)}

    def register_provider(self, provider):
        self.registered.append(deepcopy(provider))
        return {"status": "REGISTERED", "saved": True}


class ProviderOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeConnectionManager()
        self.workflow = ProviderOnboardingWorkflow(self.manager)
        self.provider = {
            "id": "sample_api",
            "name": "Sample API",
            "category": "image",
            "auth_type": "API_KEY",
            "connection_type": "API",
            "official_api_url": "https://docs.example.test/api",
            "allowed_hosts": ["api.example.test"],
            "capabilities": ["image_generation"],
            "enabled": True,
            "status": "READY",
        }

    def test_candidate_is_prepared_disabled_without_mutating_input(self):
        original = deepcopy(self.provider)
        result = self.workflow.prepare_candidate(self.provider)
        self.assertTrue(result["prepared"])
        self.assertFalse(result["provider"]["enabled"])
        self.assertEqual(result["provider"]["status"], "LOCKED")
        self.assertEqual(self.provider, original)
        self.assertEqual(self.manager.registered, [])

    def test_rejects_http_or_credential_bearing_documentation_urls(self):
        for url in ("http://docs.example.test", "https://user:pass@docs.example.test/api",
                    "https://docs.example.test/api?token=x", "https://docs.example.test/api#section"):
            with self.subTest(url=url):
                candidate = dict(self.provider, official_api_url=url)
                self.assertEqual(
                    self.workflow.prepare_candidate(candidate)["status"],
                    "OFFICIAL_HTTPS_DOCUMENTATION_REQUIRED",
                )

    def test_rejects_duplicate_or_malformed_capabilities_and_secret_values(self):
        self.assertEqual(
            self.workflow.prepare_candidate(dict(self.provider, capabilities=["image_generation"] * 2))["status"],
            "INVALID_CAPABILITIES",
        )
        self.assertEqual(
            self.workflow.prepare_candidate(dict(self.provider, api_key="secret-value"))["status"],
            "SENSITIVE_METADATA_REJECTED",
        )

    def test_probe_requires_strict_success_and_does_not_register(self):
        failed = self.workflow.verify_candidate(self.provider, lambda candidate, timeout_seconds: {"verified": "true"})
        self.assertFalse(failed["verified"])
        passed = self.workflow.verify_candidate(
            self.provider, lambda candidate, timeout_seconds: {"verified": True}
        )
        self.assertTrue(passed["verified"])
        self.assertEqual(self.manager.registered, [])

    def test_registration_is_disabled_by_default_and_does_not_claim_connectivity(self):
        result = self.workflow.register_candidate(self.provider)
        self.assertTrue(result["saved"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["status"], "REGISTERED_DISABLED")
        self.assertFalse(self.manager.registered[0]["enabled"])
        self.assertEqual(self.manager.registered[0]["status"], "LOCKED")

    def test_real_connection_manager_registers_candidate_disabled(self):
        root = Path(__file__).resolve().parents[1]
        catalog_source = root / "projects" / "provider_store" / "provider_catalog.json"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            catalog_path = base / "catalog.json"
            catalog_path.write_text(catalog_source.read_text(encoding="utf-8"), encoding="utf-8")
            manager = ConnectionManager(
                catalog_path=catalog_path,
                state_path=base / "state.json",
                audit_path=base / "audit.jsonl",
            )
            workflow = ProviderOnboardingWorkflow(manager)
            candidate = dict(
                self.provider,
                id="onboarding_test_provider",
                name="Onboarding Test Provider",
            )
            result = workflow.register_candidate(candidate)
            self.assertEqual(result["status"], "REGISTERED_DISABLED")
            self.assertTrue(result["saved"])
            stored = manager.get_provider("onboarding_test_provider")
            self.assertIsNotNone(stored)
            self.assertFalse(stored["enabled"])
            self.assertEqual(stored["status"], "LOCKED")

    def test_invalid_candidate_and_existing_provider_are_not_persisted(self):
        self.assertFalse(self.workflow.register_candidate(dict(self.provider, official_api_url="http://bad.test"))["saved"])
        self.manager.catalog["providers"].append({"id": "sample_api"})
        self.assertEqual(self.workflow.register_candidate(self.provider)["status"], "ALREADY_EXISTS")
        self.assertEqual(self.manager.registered, [])


if __name__ == "__main__":
    unittest.main()
