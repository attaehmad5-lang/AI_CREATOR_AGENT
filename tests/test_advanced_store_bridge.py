import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.advanced.advanced_store_bridge import AdvancedStoreBridge


class AdvancedStoreBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.catalog = root / "catalog.json"
        self.state = root / "state.json"
        providers = [
            {
                "id": "image-cheap", "name": "Cheap", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "capabilities": ["text_to_image"], "priority": 20,
                "regions": ["eu"], "price_per_unit": 0.02,
                "api_key": "DO_NOT_LEAK",
            },
            {
                "id": "image-fast", "name": "Fast", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "capabilities": ["text_to_image"], "priority": 1,
                "regions": ["eu"], "price_per_unit": 0.08,
            },
            {
                "id": "image-disabled", "name": "Disabled", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": False,
                "capabilities": ["text_to_image"], "priority": 0,
                "regions": ["eu"], "price_per_unit": 0.001,
            },
            {
                "id": "image-unknown-price", "name": "Unknown", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "capabilities": ["text_to_image"], "priority": 5,
                "regions": ["eu"],
            },
        ]
        self.catalog.write_text(json.dumps({"version": "1.0", "providers": providers}), encoding="utf-8")
        self.state.write_text(json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog, self.state)
        self.bridge = AdvancedStoreBridge(self.manager)

    def tearDown(self):
        self.temp.cleanup()

    def _verify(self, provider_id):
        return self.manager.verify_connectivity(provider_id, lambda provider, **kwargs: {"ok": True})

    def test_never_routes_unverified_or_disabled_provider(self):
        self.assertEqual(self.bridge.simulate("text_to_image"), [])
        self._verify("image-cheap")
        self._verify("image-disabled")
        ids = self.bridge.simulate("text_to_image")
        self.assertEqual(ids, ["image-cheap"])
        self.assertNotIn("image-disabled", ids)

    def test_priority_and_cost_routing_use_verified_shared_catalog(self):
        for pid in ("image-cheap", "image-fast", "image-unknown-price"):
            self._verify(pid)
        self.assertEqual(self.bridge.simulate("text_to_image"), [
            "image-fast", "image-unknown-price", "image-cheap"
        ])
        self.assertEqual(self.bridge.simulate("text_to_image", strategy="cost"), [
            "image-cheap", "image-fast", "image-unknown-price"
        ])
        self.assertEqual(
            self.bridge.simulate("text_to_image", strategy="cost", max_price=0.05),
            ["image-cheap"],
        )

    def test_unknown_prices_are_excluded_under_budget_ceiling(self):
        self._verify("image-unknown-price")
        self.assertEqual(
            self.bridge.simulate("text_to_image", max_price=1.0), []
        )

    def test_region_and_capability_filters_fail_closed(self):
        self._verify("image-cheap")
        self.assertEqual(self.bridge.simulate("text_to_image", region="us"), [])
        self.assertEqual(self.bridge.simulate("video_generation"), [])

    def test_metadata_is_allowlisted_and_secrets_do_not_escape(self):
        providers = self.bridge.list_providers()
        self.assertNotIn("api_key", providers[0])
        self.assertNotIn("DO_NOT_LEAK", json.dumps(providers))
        providers[0]["name"] = "mutated"
        self.assertNotEqual(self.manager.get_provider("image-cheap")["name"], "mutated")

    def test_invalid_routing_options_rejected(self):
        with self.assertRaises(ValueError):
            self.bridge.candidates("text_to_image", strategy="random")
        with self.assertRaises(ValueError):
            self.bridge.candidates("text_to_image", max_price=float("nan"))


if __name__ == "__main__":
    unittest.main()
