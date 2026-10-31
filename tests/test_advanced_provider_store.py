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


    def test_disabled_provider_cannot_be_marked_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            result = manager.store.manager.verify_connectivity(
                "advanced-audio", lambda provider, **kwargs: {"ok": True}
            )
            self.assertEqual(result["status"], "DISABLED")
            self.assertFalse(result["verified"])
            self.assertEqual(manager.status()["connected_verified"], 0)

    def test_invalid_probe_and_timeout_never_create_verified_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            connection = manager.store.manager
            invalid_probe = connection.verify_connectivity("advanced-story", None)
            invalid_timeout = connection.verify_connectivity(
                "advanced-story", lambda provider, **kwargs: {"ok": True},
                timeout_seconds=0,
            )
            self.assertEqual(invalid_probe["status"], "INVALID_PROBE")
            self.assertEqual(invalid_timeout["status"], "INVALID_TIMEOUT")
            self.assertFalse(connection.get_state("advanced-story").get("verified", False))
            self.assertEqual(manager.status()["connected_verified"], 0)

    def test_failed_provider_probe_does_not_count_as_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            result = manager.store.manager.verify_connectivity(
                "advanced-story", lambda provider, **kwargs: {"ok": False}
            )
            self.assertEqual(result["status"], "CONNECTION_FAILED")
            self.assertFalse(result["verified"])
            self.assertEqual(manager.status()["connected_verified"], 0)

    def test_probe_exception_does_not_leak_exception_message_or_mark_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))

            def failing_probe(provider, **kwargs):
                raise RuntimeError("sensitive probe detail must not be returned")

            result = manager.store.manager.verify_connectivity(
                "advanced-story", failing_probe
            )
            self.assertEqual(result["status"], "PROBE_FAILED")
            self.assertNotIn("sensitive probe detail", str(result))
            self.assertEqual(manager.status()["connected_verified"], 0)

    def test_unknown_and_core_provider_ids_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(Path(tmp))
            self.assertIsNone(manager.get_provider("missing-provider"))
            self.assertIsNone(manager.get_provider("core-image"))
            self.assertEqual(manager.by_category("not-a-real-category"), [])


if __name__ == "__main__":
    unittest.main()
