import copy
import unittest

from projects.provider_store.policy_simulator import ProviderPolicySimulator


def catalog(provider):
    return {"version": "3.0", "providers": [provider]}


def base_provider(**overrides):
    provider = {
        "id": "sample_api",
        "name": "Sample API",
        "category": "image",
        "auth_type": "API_KEY",
        "connection_type": "API",
        "enabled": False,
        "subscription_required": False,
        "official_api_url": "https://api.example.com/docs",
        "allowed_hosts": ["api.example.com"],
        "capabilities": ["text_to_image"],
    }
    provider.update(overrides)
    return provider


class ProviderPolicySimulatorTests(unittest.TestCase):
    def setUp(self):
        self.simulator = ProviderPolicySimulator()

    def test_no_change_is_passed_and_never_activates(self):
        current = catalog(base_provider())
        before = copy.deepcopy(current)
        result = self.simulator.simulate(current, copy.deepcopy(current))
        self.assertEqual(result["status"], "POLICY_SIMULATION_PASSED")
        self.assertTrue(result["schema_valid"])
        self.assertTrue(result["simulation_only"])
        self.assertFalse(result["activation_performed"])
        self.assertEqual(current, before)

    def test_enablement_is_reported_for_review_without_mutating_catalog(self):
        current = catalog(base_provider())
        proposed = catalog(base_provider(enabled=True))
        before_current, before_proposed = copy.deepcopy(current), copy.deepcopy(proposed)
        result = self.simulator.simulate(current, proposed)
        self.assertEqual(result["status"], "POLICY_SIMULATION_REVIEW_REQUIRED")
        self.assertIn("PROVIDER_ENABLEMENT_PROPOSED", {x["code"] for x in result["findings"]})
        self.assertEqual(current, before_current)
        self.assertEqual(proposed, before_proposed)
        self.assertFalse(result["activation_performed"])

    def test_enabled_api_without_official_docs_allowlist_or_capabilities_is_blocked(self):
        proposed = catalog(base_provider(
            enabled=True, official_api_url="",
            allowed_hosts=[], capabilities=[],
        ))
        result = self.simulator.simulate(catalog(base_provider()), proposed)
        codes = {x["code"] for x in result["findings"]}
        self.assertEqual(result["status"], "POLICY_SIMULATION_BLOCKED")
        self.assertIn("ENABLED_API_PROVIDER_REQUIRES_OFFICIAL_HTTPS_DOCS", codes)
        self.assertIn("ENABLED_API_PROVIDER_REQUIRES_HOST_ALLOWLIST", codes)
        self.assertIn("ENABLED_API_PROVIDER_REQUIRES_CAPABILITIES", codes)

    def test_deprecated_provider_cannot_be_simulated_as_enabled(self):
        current = catalog(base_provider())
        proposed = catalog(base_provider(
            enabled=True, deprecated=True,
            deprecation_reason="Provider retired",
            deprecation_date="2026-10-01",
        ))
        result = self.simulator.simulate(current, proposed)
        codes = {x["code"] for x in result["findings"]}
        self.assertEqual(result["status"], "POLICY_SIMULATION_BLOCKED")
        self.assertIn("DEPRECATED_PROVIDER_CANNOT_BE_ENABLED", codes)
        self.assertFalse(result["activation_performed"])

    def test_auth_allowlist_and_capability_changes_are_highlighted(self):
        current = catalog(base_provider())
        proposed = catalog(base_provider(
            auth_type="NONE", allowed_hosts=["other.example.com"], capabilities=[]
        ))
        result = self.simulator.simulate(current, proposed)
        codes = {x["code"] for x in result["findings"]}
        self.assertEqual(result["status"], "POLICY_SIMULATION_REVIEW_REQUIRED")
        self.assertIn("AUTHENTICATION_POLICY_CHANGED", codes)
        self.assertIn("ENDPOINT_ALLOWLIST_CHANGED", codes)
        self.assertIn("CAPABILITIES_REMOVED", codes)

    def test_invalid_input_and_oversized_catalog_fail_closed(self):
        invalid = self.simulator.simulate([], {})
        self.assertEqual(invalid["status"], "INVALID_INPUT")
        oversized = self.simulator.simulate(
            {"providers": []},
            {"providers": [{"id": str(i)} for i in range(501)]},
        )
        self.assertEqual(oversized["status"], "POLICY_SIMULATION_BLOCKED")
        self.assertIn("PROVIDER_LIMIT_EXCEEDED", {x["code"] for x in oversized["findings"]})

    def test_unhashable_malformed_capabilities_fail_closed(self):
        current = catalog(base_provider())
        proposed = catalog(base_provider(capabilities=[{"unexpected": "object"}]))
        result = self.simulator.simulate(current, proposed)
        self.assertEqual(result["status"], "POLICY_SIMULATION_BLOCKED")
        self.assertFalse(result["schema_valid"])

    def test_provider_removal_is_reviewed(self):
        result = self.simulator.simulate(catalog(base_provider()), {"version": "3.0", "providers": []})
        self.assertEqual(result["status"], "POLICY_SIMULATION_REVIEW_REQUIRED")
        self.assertIn("PROVIDER_REMOVED", {x["code"] for x in result["findings"]})


if __name__ == "__main__":
    unittest.main()
