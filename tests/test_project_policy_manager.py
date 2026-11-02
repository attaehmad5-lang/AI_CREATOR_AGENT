import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.project_policy_manager import ProviderProjectPolicyManager


class ProviderProjectPolicyManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "project-policies.json"
        self.manager = ProviderProjectPolicyManager(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_policy_round_trips_and_survives_reinitialization(self):
        result = self.manager.set_policy("film-project", {
            "allowed_providers": ["huggingface", "replicate"],
            "denied_providers": ["pollinations"],
            "allowed_capabilities": ["image_generation"],
            "allowed_models": ["publisher/model-v1"],
            "max_estimated_cost": 2.5,
            "automatic_fallback": False,
            "require_health_evidence": True,
            "require_verified_connection": True,
        })
        self.assertTrue(result["saved"])
        loaded = ProviderProjectPolicyManager(self.path).get_policy("film-project")
        self.assertEqual(loaded["status"], "PROJECT_POLICY_FOUND")
        self.assertEqual(loaded["policy"]["max_estimated_cost"], 2.5)
        self.assertFalse(loaded["policy"]["automatic_fallback"])
        self.assertTrue(loaded["policy"]["require_health_evidence"])

    def test_invalid_policy_fields_values_and_allow_deny_overlap_fail_closed(self):
        invalid = [
            {"unknown": True},
            {"allowed_providers": ["provider", "PROVIDER"]},
            {"allowed_providers": ["provider"], "denied_providers": ["provider"]},
            {"allowed_capabilities": ["Image Generation"]},
            {"allowed_models": ["../secret"]},
            {"max_estimated_cost": float("nan")},
            {"max_estimated_cost": 10**1000},
            {"automatic_fallback": "false"},
            {"require_health_evidence": 1},
        ]
        for policy in invalid:
            with self.subTest(policy=policy):
                result = self.manager.set_policy("film-project", policy)
                self.assertFalse(result["saved"])
                self.assertEqual(result["status"], "INVALID_PROJECT_POLICY")
        self.assertEqual(self.manager.get_policy("film-project")["status"], "PROJECT_POLICY_NOT_FOUND")

    def test_policy_updates_across_threads_preserve_all_projects(self):
        policies = [
            (f"project-{index}", {"allowed_providers": [f"provider-{index}"], "automatic_fallback": False})
            for index in range(16)
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda item: self.manager.set_policy(item[0], item[1]), policies))
        self.assertTrue(all(result["saved"] for result in results))
        reloaded = ProviderProjectPolicyManager(self.path)
        self.assertEqual(len(reloaded.data["projects"]), len(policies))
        for project_id, policy in policies:
            self.assertEqual(reloaded.get_policy(project_id)["policy"], policy)

    def test_remove_policy_and_missing_policy_are_explicit(self):
        self.manager.set_policy("film-project", {"allowed_providers": ["huggingface"]})
        self.assertTrue(self.manager.remove_policy("film-project")["removed"])
        self.assertEqual(self.manager.get_policy("film-project")["status"], "PROJECT_POLICY_NOT_FOUND")
        self.assertFalse(self.manager.remove_policy("missing")["removed"])

    def test_policy_simulation_compares_candidates_without_persisting(self):
        original_policy = {
            "allowed_providers": ["huggingface", "pollinations"],
            "automatic_fallback": True,
        }
        self.manager.set_policy("film-project", original_policy)
        before = self.path.read_bytes()
        candidate = {
            "allowed_providers": ["huggingface"],
            "denied_providers": ["pollinations"],
            "allowed_capabilities": ["image_generation"],
            "allowed_models": ["stabilityai/sdxl"],
            "max_estimated_cost": 1.0,
            "automatic_fallback": False,
            "require_health_evidence": True,
            "require_verified_connection": True,
        }
        scenarios = [
            {
                "request_id": "allowed-image",
                "provider_id": "huggingface",
                "capability": "image_generation",
                "model_id": "stabilityai/sdxl",
                "estimated_cost": 0.5,
                "health_evidence_verified": True,
                "connection_verified": True,
            },
            {
                "request_id": "denied-provider",
                "provider_id": "pollinations",
                "capability": "image_generation",
                "model_id": "stabilityai/sdxl",
                "estimated_cost": 0.5,
                "health_evidence_verified": True,
                "connection_verified": True,
            },
            {
                "request_id": "over-budget",
                "provider_id": "huggingface",
                "capability": "image_generation",
                "model_id": "stabilityai/sdxl",
                "estimated_cost": 2.0,
                "health_evidence_verified": True,
                "connection_verified": True,
            },
            {
                "request_id": "missing-health",
                "provider_id": "huggingface",
                "capability": "image_generation",
                "model_id": "stabilityai/sdxl",
                "estimated_cost": 0.5,
                "connection_verified": True,
            },
            {
                "request_id": "missing-connection",
                "provider_id": "huggingface",
                "capability": "image_generation",
                "model_id": "stabilityai/sdxl",
                "estimated_cost": 0.5,
                "health_evidence_verified": True,
            },
        ]
        result = self.manager.simulate_policy("film-project", candidate, scenarios)
        self.assertEqual(result["status"], "POLICY_SIMULATION_COMPLETED")
        self.assertFalse(result["persisted"])
        self.assertEqual(result["scenario_count"], 5)
        self.assertEqual(result["changed_decisions"], 4)
        decisions = {item["request_id"]: item for item in result["results"]}
        self.assertTrue(decisions["allowed-image"]["proposed"]["allowed"])
        self.assertFalse(decisions["denied-provider"]["proposed"]["allowed"])
        self.assertIn("PROVIDER_NOT_ALLOWLISTED", decisions["denied-provider"]["proposed"]["reasons"])
        self.assertIn("ESTIMATED_COST_LIMIT_EXCEEDED", decisions["over-budget"]["proposed"]["reasons"])
        self.assertIn("HEALTH_EVIDENCE_REQUIRED", decisions["missing-health"]["proposed"]["reasons"])
        self.assertIn("VERIFIED_CONNECTION_REQUIRED", decisions["missing-connection"]["proposed"]["reasons"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.manager.get_policy("film-project")["policy"], original_policy)

    def test_policy_simulation_reports_fallback_restriction_without_denial(self):
        result = self.manager.simulate_policy(
            "film-project",
            {"automatic_fallback": False},
            [{
                "request_id": "fallback-request",
                "provider_id": "huggingface",
                "capability": "image_generation",
                "request_fallback": True,
            }],
        )
        self.assertEqual(result["status"], "POLICY_SIMULATION_COMPLETED")
        decision = result["results"][0]["proposed"]
        self.assertTrue(decision["allowed"])
        self.assertFalse(decision["automatic_fallback_allowed"])
        self.assertTrue(decision["fallback_would_be_blocked"])

    def test_policy_simulation_rejects_invalid_candidates_and_scenarios(self):
        invalid_candidate = self.manager.simulate_policy(
            "film-project", {"max_estimated_cost": float("nan")}, [{
                "provider_id": "huggingface", "capability": "image_generation",
            }],
        )
        self.assertEqual(invalid_candidate["status"], "INVALID_CANDIDATE_POLICY")
        self.assertFalse(invalid_candidate["persisted"])

        invalid_scenarios = (
            [{"provider_id": "huggingface", "capability": "image_generation", "unknown": True}],
            [{"provider_id": "../outside", "capability": "image_generation"}],
            [{"provider_id": "huggingface", "capability": "image_generation", "estimated_cost": float("inf")}],
            [
                {"request_id": "same", "provider_id": "huggingface", "capability": "image_generation"},
                {"request_id": "same", "provider_id": "pollinations", "capability": "image_generation"},
            ],
        )
        for scenarios in invalid_scenarios:
            with self.subTest(scenarios=scenarios):
                result = self.manager.simulate_policy("film-project", {}, scenarios)
                self.assertEqual(result["status"], "INVALID_SIMULATION_SCENARIOS")
                self.assertFalse(result["persisted"])
                self.assertNotIn("results", result)

    def test_policy_simulation_requires_one_to_one_hundred_scenarios(self):
        for scenarios in ([], [{}] * 101):
            with self.subTest(count=len(scenarios)):
                result = self.manager.simulate_policy("film-project", {}, scenarios)
                self.assertEqual(result["status"], "INVALID_SIMULATION_SCENARIOS")

    def test_corrupt_store_fails_closed(self):
        self.path.write_text("{invalid-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderProjectPolicyManager(self.path)


if __name__ == "__main__":
    unittest.main()
