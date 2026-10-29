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
            "required_region": "sa-riyadh-1",
        })
        self.assertTrue(result["saved"])
        loaded = ProviderProjectPolicyManager(self.path).get_policy("film-project")
        self.assertEqual(loaded["status"], "PROJECT_POLICY_FOUND")
        self.assertEqual(loaded["policy"]["max_estimated_cost"], 2.5)
        self.assertFalse(loaded["policy"]["automatic_fallback"])
        self.assertTrue(loaded["policy"]["require_health_evidence"])
        self.assertEqual(loaded["policy"]["required_region"], "sa-riyadh-1")

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

    def test_required_region_rejects_unsafe_values(self):
        for region in ("", "sa/riyadh", "region with spaces", ["region"]):
            result = self.manager.set_policy(
                "regional-project", {"required_region": region}
            )
            self.assertFalse(result["saved"])
            self.assertEqual(result["status"], "INVALID_PROJECT_POLICY")

    def test_remove_policy_and_missing_policy_are_explicit(self):
        self.manager.set_policy("film-project", {"allowed_providers": ["huggingface"]})
        self.assertTrue(self.manager.remove_policy("film-project")["removed"])
        self.assertEqual(self.manager.get_policy("film-project")["status"], "PROJECT_POLICY_NOT_FOUND")
        self.assertFalse(self.manager.remove_policy("missing")["removed"])

    def test_corrupt_store_fails_closed(self):
        self.path.write_text("{invalid-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderProjectPolicyManager(self.path)


if __name__ == "__main__":
    unittest.main()
