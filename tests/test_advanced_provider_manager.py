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

    def test_production_catalog_consistency_reports_match(self):
        result = AdvancedProviderManager().catalog_consistency()
        self.assertEqual(result["status"], "MATCH", result)
        self.assertEqual(result["providers"], 18)
        self.assertEqual(result["canonical_advanced_providers"], 18)
        self.assertEqual(result["differences"], [])

    def test_catalog_consistency_reports_metadata_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "catalog.json"
            state = root / "state.json"
            categories = root / "categories.json"
            advanced_catalog = root / "advanced.json"
            provider = {
                "id": "story_test", "name": "Story Test", "category": "script_story",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "subscription_required": False, "capabilities": ["story_generation"],
            }
            catalog.write_text(json.dumps({"version": "2.0", "providers": [provider]}), encoding="utf-8")
            state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            categories.write_text(json.dumps({"categories": {"script_story": {"name": "Script & Story", "capabilities": ["story_generation"]}}}), encoding="utf-8")
            legacy = dict(provider, auth_type="API_KEY")
            advanced_catalog.write_text(json.dumps({"providers": [legacy]}), encoding="utf-8")
            connection = ConnectionManager(catalog, state)
            manager = AdvancedProviderManager(connection_manager=connection, categories_path=str(categories), advanced_catalog_path=str(advanced_catalog))
            result = manager.catalog_consistency()
            self.assertEqual(result["status"], "MISMATCH")
            self.assertIn({"provider": "story_test", "field": "auth_type", "issue": "metadata_mismatch"}, result["differences"])

    def test_production_readiness_report_matches_catalog_and_explains_locked_providers(self):
        advanced = AdvancedProviderManager()
        report = advanced.readiness_report()
        self.assertEqual(report["catalog_consistency"], "MATCH")
        self.assertEqual(report["providers_total"], 18)
        self.assertEqual(report["providers_ready"], 0)
        self.assertEqual(report["status"], "DEGRADED")
        self.assertTrue(all(not item["ready"] for item in report["providers"]))
        self.assertTrue(any("CATALOG_LOCKED" in item["blockers"] for item in report["providers"]))
        encoded = json.dumps(report).casefold()
        self.assertNotIn("api_key", encoded)
        self.assertNotIn("token_value", encoded)

    def test_category_catalog_is_validated_and_returns_detached_records(self):
        advanced = AdvancedProviderManager()
        categories = advanced.list_categories()
        self.assertEqual(len(categories), 6)
        audio = advanced.get_category("advanced_audio")
        self.assertIn("audio_generation", audio["capabilities"])
        audio["capabilities"].append("caller_mutation")
        self.assertNotIn("caller_mutation", advanced.get_category("advanced_audio")["capabilities"])
        self.assertIsNone(advanced.get_category("../invalid"))

    def test_invalid_category_catalog_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "categories.json"
            bad_path.write_text("{invalid", encoding="utf-8")
            advanced = AdvancedProviderManager(categories_path=str(bad_path))
            self.assertEqual(advanced.category_catalog()["status"], "CATEGORY_CATALOG_UNAVAILABLE")
            self.assertEqual(advanced.list_categories(), [])

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
        text_ledger = ledger.with_name("ADVANCED_PROVIDER_STORE_COMPLETION_REMAINING_LEDGER.txt")
        text_content = text_ledger.read_text(encoding="utf-8")
        text_ids = re.findall(r"^Feature (\d{3}): .+$", text_content, re.MULTILINE)
        self.assertEqual(text_ids, [f"{feature_id:03d}" for feature_id in range(1, 151)])
        self.assertEqual(len(re.findall(r"^Status: (PARTIAL|NOT STARTED|UNVERIFIED|COMPLETED|BLOCKED)$", text_content, re.MULTILINE)), 150)

    def test_readiness_report_separates_locked_catalog_and_unverified_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            report = advanced.readiness_report()
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["providers_total"], 3)
            story = next(item for item in report["providers"] if item["provider"] == "story_test")
            self.assertIn("CONNECTION_NOT_VERIFIED", story["blockers"])
            self.assertEqual(story["credential_status"], "NO_CREDENTIAL_REQUIRED")
            locked = next(item for item in report["providers"] if item["provider"] == "story_locked")
            self.assertIn("CATALOG_LOCKED", locked["blockers"])
            self.assertFalse(locked["ready"])
            self.assertEqual(report["catalog_consistency"], "MISMATCH")

    def test_advanced_facade_blocks_disabled_and_locked_provider_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            called = []
            result = advanced.load_provider("story_disabled", lambda provider: called.append(provider))
            self.assertEqual(result["status"], "DISABLED")
            self.assertFalse(result["loaded"])
            self.assertEqual(called, [])
            locked = advanced.load_provider("story_locked", lambda provider: called.append(provider))
            self.assertEqual(locked["status"], "LOCKED")
            self.assertEqual(called, [])
            missing = advanced.load_provider("core_only", lambda provider: object())
            self.assertEqual(missing["status"], "NOT_FOUND")

    def test_advanced_facade_rejects_core_provider_unload_and_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            self.assertEqual(advanced.unload_provider("core_only")["status"], "NOT_FOUND")
            self.assertIsNone(advanced.loaded_provider("core_only"))

    def test_readiness_report_never_contains_raw_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            report = json.dumps(advanced.readiness_report()).casefold()
            self.assertNotIn("secret", report)
            self.assertNotIn("api_key", report)

    def test_diagnostics_never_returns_credential_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, advanced = self._manager(Path(tmp))
            diagnostics = advanced.provider_diagnostics("story_test")
            self.assertNotIn("token", diagnostics)
            self.assertNotIn("api_key", diagnostics)
            self.assertEqual(advanced.provider_diagnostics("missing")["status"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
