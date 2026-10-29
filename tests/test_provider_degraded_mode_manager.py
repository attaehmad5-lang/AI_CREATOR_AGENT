import unittest

from projects.provider_store.provider_degraded_mode_manager import ProviderDegradedModeManager


class ProviderDegradedModeManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = ProviderDegradedModeManager()

    def test_normal_mode_requires_all_required_capabilities_healthy(self):
        result = self.manager.evaluate(
            required_categories=["image", "text"],
            providers=[
                {"provider_id": "provider-a", "state": "HEALTHY", "categories": ["image"]},
                {"provider_id": "provider-b", "state": "HEALTHY", "categories": ["text"]},
            ],
        )
        self.assertEqual(result["mode"], "NORMAL")
        self.assertFalse(result["fail_closed"])
        self.assertFalse(result["execution_started"])

    def test_partial_outage_enters_degraded_mode_without_bypassing_policy(self):
        result = self.manager.evaluate(
            required_categories=["image", "video"],
            providers=[
                {"provider_id": "image-a", "state": "HEALTHY", "categories": ["image"]},
                {"provider_id": "video-a", "state": "UNHEALTHY", "categories": ["video"]},
            ],
        )
        self.assertEqual(result["mode"], "DEGRADED")
        self.assertEqual(result["missing_categories"], ["video"])
        self.assertIn("tenant", result["recommendation"])
        self.assertFalse(result["execution_started"])

    def test_degraded_provider_is_not_misreported_as_healthy(self):
        result = self.manager.evaluate(
            required_categories=["image"],
            providers=[{"provider_id": "image-a", "state": "DEGRADED", "categories": ["image"]}],
        )
        self.assertEqual(result["mode"], "DEGRADED")
        self.assertEqual(result["degraded_only_categories"], ["image"])
        self.assertEqual(result["healthy_providers_by_category"]["image"], [])

    def test_total_outage_fails_closed(self):
        result = self.manager.evaluate(
            required_categories=["image", "video"],
            providers=[{"provider_id": "image-a", "state": "UNHEALTHY", "categories": ["image"]}],
        )
        self.assertEqual(result["mode"], "OUTAGE")
        self.assertTrue(result["fail_closed"])
        self.assertEqual(set(result["missing_categories"]), {"image", "video"})

    def test_malformed_or_duplicate_provider_data_fails_closed(self):
        for providers in (
            [{"provider_id": "a", "state": "HEALTHY", "categories": ["image"]},
             {"provider_id": "a", "state": "HEALTHY", "categories": ["image"]}],
            [{"provider_id": "a", "state": [], "categories": ["image"]}],
            [{"provider_id": "a", "state": "HEALTHY", "categories": "image"}],
            ["not-an-object"],
        ):
            result = self.manager.evaluate(required_categories=["image"], providers=providers)
            self.assertEqual(result["mode"], "UNKNOWN")
            self.assertTrue(result["fail_closed"])

    def test_invalid_required_categories_fail_closed(self):
        for required in ([], ["image", "image"], ["image", None], "image"):
            result = self.manager.evaluate(required_categories=required, providers=[])
            self.assertEqual(result["mode"], "UNKNOWN")
            self.assertTrue(result["fail_closed"])


if __name__ == "__main__":
    unittest.main()
