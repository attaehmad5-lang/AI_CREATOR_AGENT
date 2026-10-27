"""Focused regression tests for the advanced provider-store facade.

These tests are intentionally offline: they exercise catalog selection and state
separation without making provider API calls or spending credits.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.advanced.advanced_provider_manager import AdvancedProviderManager
from projects.provider_store.unified_provider_registry import ADVANCED_CATEGORIES


ROOT = Path(__file__).resolve().parents[1]
STORE_DIR = ROOT / "projects" / "provider_store"
ADVANCED_DIR = STORE_DIR / "advanced"


class AdvancedProviderManagerTests(unittest.TestCase):
    def test_advanced_catalog_matches_central_catalog_by_id_and_category(self):
        central = json.loads((STORE_DIR / "provider_catalog.json").read_text(encoding="utf-8"))
        advanced = json.loads(
            (ADVANCED_DIR / "advanced_provider_catalog.json").read_text(encoding="utf-8")
        )
        central_advanced = {
            provider["id"]: provider
            for provider in central["providers"]
            if provider.get("category") in ADVANCED_CATEGORIES
        }
        standalone_advanced = {provider["id"]: provider for provider in advanced["providers"]}

        self.assertEqual(set(central_advanced), set(standalone_advanced))
        for provider_id, provider in standalone_advanced.items():
            with self.subTest(provider_id=provider_id):
                self.assertEqual(provider["category"], central_advanced[provider_id]["category"])

    def test_advanced_catalog_has_expected_category_coverage(self):
        catalog = json.loads(
            (ADVANCED_DIR / "advanced_provider_catalog.json").read_text(encoding="utf-8")
        )
        category_counts = {}
        for provider in catalog["providers"]:
            category = provider.get("category")
            category_counts[category] = category_counts.get(category, 0) + 1

        self.assertEqual(set(category_counts), ADVANCED_CATEGORIES)
        self.assertEqual(sum(category_counts.values()), 18)
        self.assertTrue(all(count == 3 for count in category_counts.values()))

    def test_manager_filters_core_providers_and_exposes_all_advanced_providers(self):
        manager = AdvancedProviderManager()
        providers = manager.list_providers()
        self.assertEqual(len(providers), 18)
        self.assertTrue(all(p.get("category") in ADVANCED_CATEGORIES for p in providers))
        self.assertEqual(len({p.get("id") for p in providers}), len(providers))

    def test_get_provider_returns_none_for_unknown_and_core_provider(self):
        manager = AdvancedProviderManager()
        core_provider = next(
            p for p in manager.store.list_providers()
            if p.get("category") not in ADVANCED_CATEGORIES
        )
        self.assertIsNone(manager.get_provider("provider-that-does-not-exist"))
        self.assertIsNone(manager.get_provider(core_provider["id"]))

    def test_by_category_only_accepts_advanced_categories(self):
        manager = AdvancedProviderManager()
        for category in ADVANCED_CATEGORIES:
            with self.subTest(category=category):
                providers = manager.by_category(category)
                self.assertEqual(len(providers), 3)
                self.assertTrue(all(p.get("category") == category for p in providers))
        self.assertEqual(manager.by_category("not_a_real_category"), [])

    def test_status_count_matches_filtered_catalog(self):
        manager = AdvancedProviderManager()
        status = manager.status()
        self.assertEqual(status["providers"], len(manager.list_providers()))
        self.assertEqual(status["providers"], 18)
        self.assertEqual(status["status"], "READY")
        self.assertTrue(status["ready"])

    def test_rejects_mixing_connection_manager_with_explicit_paths(self):
        from projects.provider_store.connection_manager import ConnectionManager

        manager = ConnectionManager()
        with self.assertRaises(ValueError):
            AdvancedProviderManager(manager, catalog_path="catalog.json")

    def test_explicit_catalog_and_state_paths_are_supported(self):
        central_path = STORE_DIR / "provider_catalog.json"
        catalog = json.loads(central_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog_path = root / "catalog.json"
            state_path = root / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(
                json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}),
                encoding="utf-8",
            )
            manager = AdvancedProviderManager(
                catalog_path=str(catalog_path), state_path=str(state_path)
            )
            self.assertEqual(len(manager.list_providers()), 18)
            self.assertEqual(manager.status()["providers"], 18)


if __name__ == "__main__":
    unittest.main()
