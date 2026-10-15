import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_store_manager import ProviderStoreManager
from projects.provider_store.category_manager import CategoryManager
from projects.provider_store.advanced.advanced_provider_manager import AdvancedProviderManager
from projects.provider_store.unified_provider_registry import UnifiedProviderRegistry
from projects.provider_store.unified_provider_router import UnifiedProviderRouter
from projects.provider_store.provider_status_manager import ProviderStatusManager
from projects.provider_store.provider_unlock_manager import ProviderUnlockManager
from projects.provider_store.unlock_flow_manager import UnlockFlowManager
from projects.provider_store.provider_flow import ProviderFlow


class ProviderStoreCompatibilityTests(unittest.TestCase):
    def test_legacy_store_facade_uses_central_catalog_and_live_connection_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(json.dumps({
                "providers": [{
                    "id": "test-image", "name": "Test Image", "category": "image",
                    "auth_type": "NONE", "connection_type": "API", "enabled": True,
                    "status": "CONNECTED",
                }]
            }), encoding="utf-8")
            state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            connection_manager = ConnectionManager(catalog, state)
            store = ProviderStoreManager(connection_manager=connection_manager)
            self.assertEqual(len(store.list_providers()), 1)
            self.assertEqual(store.get_provider("test-image")["catalog_status"], "CONNECTED")
            self.assertEqual(store.status()["connected_verified"], 0)
            status_manager = ProviderStatusManager(connection_manager=connection_manager)
            unlock_manager = ProviderUnlockManager(connection_manager=connection_manager)
            unlock_flow = UnlockFlowManager(connection_manager=connection_manager)
            flow = ProviderFlow(manager=unlock_flow)
            self.assertEqual(status_manager.get_status("test-image"), "UNKNOWN")
            self.assertFalse(status_manager.is_available("test-image"))
            info = unlock_manager.connection_info("test-image")
            self.assertEqual(info["catalog_status"], "CONNECTED")
            self.assertEqual(info["status"], "UNKNOWN")
            self.assertFalse(info["verified"])
            prepared = unlock_flow.prepare("test-image")
            self.assertEqual(flow.available_providers()[0]["id"], "test-image")
            self.assertEqual(prepared["catalog_status"], "CONNECTED")
            self.assertEqual(prepared["current_status"], "UNKNOWN")
            self.assertFalse(prepared["verified"])

            verified = connection_manager.verify_connectivity(
                "test-image", lambda provider, **kwargs: {"ok": True}
            )
            self.assertTrue(verified["verified"])
            self.assertEqual(store.status()["connected_verified"], 1)
            self.assertEqual(flow.available_providers()[0]["status"], "CONNECTED")
            self.assertTrue(flow.available_providers()[0]["verified"])
            self.assertEqual(status_manager.get_status("test-image"), "CONNECTED")
            self.assertTrue(status_manager.is_available("test-image"))
            self.assertEqual(unlock_manager.connection_info("test-image")["status"], "CONNECTED")
            self.assertTrue(unlock_flow.prepare("test-image")["verified"])
            self.assertEqual(store.catalog["providers"][0]["catalog_status"], "CONNECTED")

    def test_legacy_registry_category_and_advanced_views_share_one_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "catalog.json"
            state = root / "state.json"
            catalog.write_text(json.dumps({
                "providers": [
                    {
                        "id": "image", "name": "Image", "category": "image",
                        "auth_type": "NONE", "connection_type": "API", "enabled": True,
                        "status": "CONNECTED",
                    },
                    {
                        "id": "story", "name": "Story", "category": "script_story",
                        "auth_type": "API_KEY", "connection_type": "API", "enabled": False,
                        "capabilities": ["story_generation"],
                    },
                ]
            }), encoding="utf-8")
            state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            manager = ConnectionManager(catalog, state)
            store = ProviderStoreManager(connection_manager=manager)
            registry = UnifiedProviderRegistry(connection_manager=manager)
            advanced = AdvancedProviderManager(connection_manager=manager)
            categories = CategoryManager(store=store)
            self.assertEqual(len(registry.all_providers()), 2)
            self.assertEqual(registry.status()["advanced_providers"], 1)
            self.assertEqual(len(registry.core["providers"]), 1)
            self.assertEqual(len(registry.advanced["providers"]), 1)
            self.assertEqual([p["id"] for p in advanced.catalog["providers"]], ["story"])
            self.assertEqual([p["id"] for p in advanced.list_providers()], ["story"])
            self.assertEqual([p["id"] for p in categories.providers_by_category("image")], ["image"])

            router = UnifiedProviderRouter(connection_manager=manager)
            before = router.route("text_to_image")
            self.assertEqual(before["status"], "PROVIDERS_FOUND")
            self.assertEqual(before["available"], [])
            self.assertEqual(before["unverified"][0]["id"], "image")
            manager.verify_connectivity("image", lambda provider, **kwargs: {"ok": True})
            after = router.route("text_to_image")
            self.assertEqual(after["status"], "ROUTABLE")
            self.assertFalse(after["route_selected"])
            self.assertEqual(after["available"][0]["id"], "image")

    def test_production_registry_contains_all_advanced_provider_entries(self):
        project_root = Path(__file__).resolve().parents[1]
        main_catalog = project_root / "projects" / "provider_store" / "provider_catalog.json"
        advanced_catalog = project_root / "projects" / "provider_store" / "advanced" / "advanced_provider_catalog.json"
        main_ids = {p["id"] for p in json.loads(main_catalog.read_text(encoding="utf-8"))["providers"]}
        advanced_ids = {p["id"] for p in json.loads(advanced_catalog.read_text(encoding="utf-8"))["providers"]}
        self.assertEqual(len(main_ids), 55)
        self.assertEqual(len(advanced_ids), 18)
        self.assertTrue(advanced_ids.issubset(main_ids))

        with tempfile.TemporaryDirectory() as tmp:
            manager = ConnectionManager(main_catalog, Path(tmp) / "state.json")
            registry = UnifiedProviderRegistry(connection_manager=manager)
            advanced = AdvancedProviderManager(connection_manager=manager)
            self.assertEqual(len(registry.all_providers()), 55)
            self.assertEqual(len(advanced.list_providers()), 18)
            self.assertEqual(registry.status()["advanced_providers"], 18)

    def test_store_facade_rejects_mixed_manager_and_path_arguments(self):
        with self.assertRaises(ValueError):
            ProviderStoreManager(
                catalog_path="catalog.json",
                connection_manager=ConnectionManager(),
            )


if __name__ == "__main__":
    unittest.main()
