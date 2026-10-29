import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.advanced.advanced_provider_manager import AdvancedProviderManager


class AdvancedProviderStoreTests(unittest.TestCase):
    def _manager(self, root: Path) -> AdvancedProviderManager:
        catalog = root / "catalog.json"
        state = root / "state.json"
        catalog.write_text(json.dumps({
            "store": "AI_CREATOR_AGENT_PROVIDER_STORE",
            "version": "3.0",
            "status": "READY",
            "subscription_policy": "USER_CONTROLLED",
            "providers": [
                {
                    "id": "advanced-story", "name": "Advanced Story",
                    "category": "script_story", "auth_type": "NONE",
                    "connection_type": "API", "enabled": True,
                    "subscription_required": False,
                    "allowed_hosts": ["story.example.test"],
                    "official_api_url": "https://story.example.test/docs",
                    "capabilities": ["story_generation"],
                },
                {
                    "id": "advanced-audio", "name": "Advanced Audio",
                    "category": "advanced_audio", "auth_type": "NONE",
                    "connection_type": "API", "enabled": False,
                    "subscription_required": False,
                    "capabilities": ["audio_generation"],
                },
                {
                    "id": "core-image", "name": "Core Image",
                    "category": "image", "auth_type": "NONE",
                    "connection_type": "API", "enabled": True,
                    "subscription_required": False,
                    "allowed_hosts": ["image.example.test"],
                    "official_api_url": "https://image.example.test/docs",
                    "capabilities": ["text_to_image"],
                },
            ],
        }), encoding="utf-8")
        state.write_text(json.dumps({
            "version": "1.0", "policy": "USER_CONTROLLED", "providers": {}
        }), encoding="utf-8")
        return AdvancedProviderManager(catalog_path=str(catalog), state_path=str(state))

    def test_only_advanced_categories_are_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            self.assertEqual(
                {provider["id"] for provider in manager.list_providers()},
                {"advanced-story", "advanced-audio"},
            )
            self.assertIsNone(manager.get_provider("core-image"))
            self.assertEqual(manager.get_provider("advanced-story")["id"], "advanced-story")
            self.assertEqual(manager.by_category("script_story")[0]["id"], "advanced-story")
            self.assertEqual(manager.by_category("image"), [])

    def test_status_distinguishes_catalog_readiness_from_live_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            initial = manager.status()
            self.assertEqual(initial["status"], "READY")
            self.assertTrue(initial["catalog_valid"])
            self.assertEqual(initial["providers"], 2)
            self.assertEqual(initial["providers_enabled"], 1)
            self.assertEqual(initial["providers_disabled"], 1)
            self.assertEqual(initial["connected_verified"], 0)

            result = manager.store.manager.verify_connectivity(
                "advanced-story", lambda provider, **kwargs: {"ok": True}
            )
            self.assertTrue(result["verified"], result)
            updated = manager.status()
            self.assertEqual(updated["connected_verified"], 1)
            self.assertEqual(updated["providers_enabled"], 1)
            self.assertEqual(updated["status"], "READY")

    def test_constructor_rejects_ambiguous_manager_and_path_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self._manager(root)
            with self.assertRaises(ValueError):
                AdvancedProviderManager(
                    connection_manager=manager.store.manager,
                    catalog_path=str(root / "catalog.json"),
                )

    def test_catalog_view_is_advanced_only_and_status_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            self.assertEqual(manager.catalog["status"], "READY")
            self.assertEqual(
                {item["id"] for item in manager.catalog["providers"]},
                {"advanced-story", "advanced-audio"},
            )
            status = manager.status()
            self.assertIn("catalog_valid", status)
            self.assertIn("connected_verified", status)


if __name__ == "__main__":
    unittest.main()
