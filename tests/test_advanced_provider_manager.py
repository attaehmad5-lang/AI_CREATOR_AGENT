"""Focused tests for advanced provider filtering and runtime readiness."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.advanced.advanced_provider_manager import AdvancedProviderManager


class AdvancedProviderManagerTests(unittest.TestCase):
    def _manager(self, root: Path) -> tuple[ConnectionManager, AdvancedProviderManager]:
        catalog_path = root / "catalog.json"
        state_path = root / "state.json"
        catalog_path.write_text(json.dumps({
            "version": "2.0",
            "providers": [
                {
                    "id": "story_test",
                    "name": "Story Test",
                    "category": "script_story",
                    "auth_type": "NONE",
                    "connection_type": "API",
                    "enabled": True,
                    "status": "CONNECTED",
                    "capabilities": ["story_generation", "dialogue_writing"],
                },
                {
                    "id": "image_test",
                    "name": "Image Test",
                    "category": "image",
                    "auth_type": "NONE",
                    "connection_type": "API",
                    "enabled": True,
                    "capabilities": ["image_generation"],
                },
                {
                    "id": "story_locked",
                    "name": "Locked Story",
                    "category": "script_story",
                    "auth_type": "NONE",
                    "connection_type": "API",
                    "enabled": True,
                    "status": "LOCKED",
                    "capabilities": ["story_generation"],
                },
                {
                    "id": "story_disabled",
                    "name": "Disabled Story",
                    "category": "script_story",
                    "auth_type": "NONE",
                    "connection_type": "API",
                    "enabled": False,
                    "capabilities": ["story_generation"],
                },
            ],
        }), encoding="utf-8")
        state_path.write_text(json.dumps({"version": "1.0", "providers": {}}), encoding="utf-8")
        connection = ConnectionManager(catalog_path, state_path)
        return connection, AdvancedProviderManager(connection_manager=connection)

    def test_catalog_label_does_not_count_as_verified_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection, advanced = self._manager(Path(tmp))
            self.assertEqual(advanced.list_verified_enabled_providers(), [])
            diagnostics = advanced.provider_diagnostics("story_test")
            self.assertEqual(diagnostics["catalog_status"], "CONNECTED")
            self.assertEqual(diagnostics["runtime_status"], "UNKNOWN")
            self.assertFalse(diagnostics["verified"])
            self.assertEqual(advanced.status()["connected_verified"], 0)

            result = connection.verify_connectivity(
                "story_test", lambda provider, **kwargs: {"ok": True}
            )
            self.assertTrue(result["verified"])
            self.assertEqual(
                [p["id"] for p in advanced.list_verified_enabled_providers()],
                ["story_test"],
            )
            self.assertEqual(
                [p["id"] for p in advanced.list_verified_enabled_providers("story_generation")],
                ["story_test"],
            )
            self.assertEqual(
                [p["id"] for p in advanced.list_verified_enabled_providers("dialogue_writing")],
                ["story_test"],
            )
            self.assertEqual(advanced.list_verified_enabled_providers("image_generation"), [])
            self.assertEqual(advanced.status()["connected_verified"], 1)

    def test_verified_filter_excludes_disabled_and_rejects_invalid_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            connection, advanced = self._manager(Path(tmp))
            connection.verify_connectivity("story_test", lambda provider, **kwargs: True)
            connection.verify_connectivity("image_test", lambda provider, **kwargs: True)
            connection.verify_connectivity("story_locked", lambda provider, **kwargs: True)
            self.assertEqual(
                [p["id"] for p in advanced.list_verified_enabled_providers()],
                ["story_test"],
            )
            self.assertEqual(advanced.list_verified_enabled_providers("  "), [])
            self.assertEqual(advanced.list_verified_enabled_providers(123), [])

    def test_completion_ledger_contains_150_unique_feature_rows(self):
        ledger = (
            Path(__file__).resolve().parents[1]
            / "projects" / "provider_store" / "advanced"
            / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md"
        )
        content = ledger.read_text(encoding="utf-8")
        import re
        rows = re.findall(r"^\| (\d{3}) \| .+? \| (PARTIAL|NOT STARTED|UNVERIFIED|COMPLETED|BLOCKED) \|", content, re.MULTILINE)
        self.assertEqual(len(rows), 150)
        self.assertEqual([int(feature_id) for feature_id, _ in rows], list(range(1, 151)))

    def test_diagnostics_never_returns_credential_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            diagnostics = advanced.provider_diagnostics("story_test")
            self.assertNotIn("token", diagnostics)
            self.assertNotIn("api_key", diagnostics)
            self.assertEqual(advanced.provider_diagnostics("missing")["status"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
