import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_store_manager import ProviderStoreManager


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

            verified = connection_manager.verify_connectivity(
                "test-image", lambda provider, **kwargs: {"ok": True}
            )
            self.assertTrue(verified["verified"])
            self.assertEqual(store.status()["connected_verified"], 1)
            self.assertEqual(store.catalog["providers"][0]["catalog_status"], "CONNECTED")

    def test_store_facade_rejects_mixed_manager_and_path_arguments(self):
        with self.assertRaises(ValueError):
            ProviderStoreManager(
                catalog_path="catalog.json",
                connection_manager=ConnectionManager(),
            )


if __name__ == "__main__":
    unittest.main()
