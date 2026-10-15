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

    def test_denied_capability_validation_matrix_50_cases(self):
        valid_cases = [
            [f"capability_{index}"] for index in range(25)
        ]
        invalid_cases = [
            None,
            "image_generation",
            {},
            (),
            ["Image"],
            ["image generation"],
            ["image-generation"],
            ["1image"],
            ["image/voice"],
            ["image.voice"],
            ["image:voice"],
            ["image+voice"],
            ["é"],
            ["a" * 81],
            ["image", "IMAGE"],
            ["image", None],
            [1],
            [False],
            [["nested"]],
            [{"capability": "image"}],
            [""],
            ["UPPER_CASE"],
            ["white space"],
            [f"capability_{index}" for index in range(101)],
            [f"valid_{index}" for index in range(100)] + ["invalid-value"],
        ]
        self.assertEqual(len(valid_cases) + len(invalid_cases), 50)
        for index, capabilities in enumerate(valid_cases):
            with self.subTest(case=index, capabilities=capabilities):
                result = self.manager.set_policy(
                    f"valid-project-{index}", {"denied_capabilities": capabilities}
                )
                self.assertTrue(result["saved"], result)
                loaded = self.manager.get_policy(f"valid-project-{index}")
                self.assertEqual(loaded["policy"]["denied_capabilities"], capabilities)
        for index, capabilities in enumerate(invalid_cases):
            with self.subTest(case=index + 25, capabilities=repr(capabilities)[:100]):
                result = self.manager.set_policy(
                    f"invalid-project-{index}", {"denied_capabilities": capabilities}
                )
                self.assertFalse(result["saved"], result)
                self.assertEqual(result["status"], "INVALID_PROJECT_POLICY")

    def test_allowed_and_denied_capability_overlap_is_rejected(self):
        result = self.manager.set_policy(
            "film-project",
            {"allowed_capabilities": ["image_generation"], "denied_capabilities": ["image_generation"]},
        )
        self.assertFalse(result["saved"])
        self.assertEqual(result["status"], "INVALID_PROJECT_POLICY")
        self.assertTrue(any("capability cannot be both allowed and denied" in error for error in result["errors"]))

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

    def test_corrupt_store_fails_closed(self):
        self.path.write_text("{invalid-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderProjectPolicyManager(self.path)


if __name__ == "__main__":
    unittest.main()
