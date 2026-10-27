import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.catalog_interchange import (
    _contains_credential_material,
    create_catalog_snapshot,
    export_catalog,
    import_catalog,
    list_catalog_snapshots,
    restore_catalog_snapshot,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "projects" / "provider_store" / "provider_catalog.json"


class CatalogInterchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.json"
        self.source.write_bytes(CATALOG.read_bytes())

    def tearDown(self):
        self.temp.cleanup()

    def test_export_and_import_round_trip_valid_catalog(self):
        exported = self.root / "exports" / "catalog.json"
        result = export_catalog(self.source, exported)
        self.assertTrue(result["exported"])
        self.assertGreater(result["provider_count"], 0)

        target = self.root / "active" / "catalog.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"version": "3.0", "providers": []}), encoding="utf-8"
        )
        imported = import_catalog(exported, target)
        self.assertTrue(imported["imported"])
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            json.loads(exported.read_text(encoding="utf-8")),
        )
        self.assertTrue(Path(str(target) + ".bak").is_file())
        self.assertEqual(
            json.loads(Path(str(target) + ".bak").read_text(encoding="utf-8"))[
                "providers"
            ],
            [],
        )

    def test_credential_bearing_source_is_rejected_without_mutation(self):
        invalid = self.root / "invalid.json"
        catalog = json.loads(self.source.read_text(encoding="utf-8"))
        catalog["providers"][0]["api_key"] = "never-export"
        invalid.write_text(json.dumps(catalog), encoding="utf-8")
        target = self.root / "target.json"
        original = b'{"version":"3.0","providers":[]}\n'
        target.write_bytes(original)
        result = import_catalog(invalid, target)
        self.assertEqual(result["status"], "CATALOG_IMPORT_INVALID_SOURCE")
        self.assertFalse(result["imported"])
        self.assertEqual(target.read_bytes(), original)

    def test_export_refuses_credential_bearing_catalog(self):
        catalog = json.loads(self.source.read_text(encoding="utf-8"))
        catalog["providers"][0]["nested"] = {"refresh_token": "do-not-export"}
        secret_source = self.root / "secret-source.json"
        secret_source.write_text(json.dumps(catalog), encoding="utf-8")
        target = self.root / "secret-export.json"
        with self.assertRaises(ValueError):
            export_catalog(secret_source, target)
        self.assertFalse(target.exists())
        self.assertTrue(
            _contains_credential_material({"nested": {"refresh_token": "x"}})
        )
        self.assertFalse(
            _contains_credential_material({"auth_header_name": "x-api-key"})
        )

    def test_failed_atomic_write_does_not_leave_partial_target(self):
        target = self.root / "new-target.json"
        with patch(
            "projects.provider_store.catalog_interchange.atomic_replace_with_retry",
            side_effect=OSError("simulated replace failure"),
        ):
            result = import_catalog(self.source, target, backup=False)
        self.assertFalse(result["imported"])
        self.assertFalse(target.exists())
        self.assertEqual(result["status"], "CATALOG_IMPORT_FAILED_ROLLED_BACK")

    def test_versioned_snapshot_listing_and_restore(self):
        history = self.root / "history"
        snapshot_result = create_catalog_snapshot(self.source, history)
        self.assertTrue(snapshot_result["created"])
        snapshot_path = history / snapshot_result["snapshot"]

        listing = list_catalog_snapshots(history)
        self.assertEqual(listing["status"], "CATALOG_HISTORY_LISTED")
        self.assertEqual(listing["snapshots"][0]["name"], snapshot_path.name)

        target = self.root / "active.json"
        target.write_text(
            json.dumps({"version": "3.0", "providers": []}), encoding="utf-8"
        )
        restored = restore_catalog_snapshot(snapshot_path, target)
        self.assertTrue(restored["restored"])
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            json.loads(self.source.read_text(encoding="utf-8")),
        )
        self.assertTrue(Path(str(target) + ".bak").is_file())

    def test_snapshot_listing_ignores_invalid_files_and_empty_history_is_safe(self):
        history = self.root / "history"
        self.assertEqual(
            list_catalog_snapshots(history)["status"], "CATALOG_HISTORY_EMPTY"
        )
        history.mkdir()
        (history / "catalog-broken.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(list_catalog_snapshots(history)["snapshots"], [])

    def test_export_refuses_malformed_json(self):
        invalid = self.root / "broken.json"
        invalid.write_text("{broken", encoding="utf-8")
        target = self.root / "export.json"
        with self.assertRaises(ValueError):
            export_catalog(invalid, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
