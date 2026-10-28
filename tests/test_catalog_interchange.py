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
    prune_catalog_snapshots,
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

    def test_snapshot_prune_defaults_to_preview_and_preserves_invalid_files(self):
        history = self.root / "history"
        history.mkdir()
        for day in range(1, 5):
            (history / f"catalog-2026010{day}T000000000000Z.json").write_bytes(
                self.source.read_bytes()
            )
        invalid = history / "catalog-20260109T000000000000Z.json"
        invalid.write_text("{invalid", encoding="utf-8")

        preview = prune_catalog_snapshots(history, keep=2)
        self.assertFalse(preview["applied"])
        self.assertEqual(len(preview["would_delete"]), 2)
        self.assertEqual(preview["deleted"], [])
        self.assertTrue(
            all((history / name).is_file() for name in preview["would_delete"])
        )
        self.assertTrue(invalid.is_file())

        applied = prune_catalog_snapshots(history, keep=2, apply=True)
        self.assertTrue(applied["applied"])
        self.assertEqual(len(applied["deleted"]), 2)
        self.assertEqual(len(list(history.glob("catalog-*.json"))), 3)
        self.assertTrue(invalid.is_file())

    def test_snapshot_prune_rejects_unsafe_retention_limits(self):
        for keep in (0, -1, 10001, True, 1.5):
            with self.subTest(keep=keep), self.assertRaises(ValueError):
                prune_catalog_snapshots(self.root, keep=keep)
        with self.assertRaises(ValueError):
            prune_catalog_snapshots(self.root, apply="yes")

    def test_snapshot_listing_and_restore_reject_symlinked_snapshots(self):
        history = self.root / "history"
        history.mkdir()
        link = history / "catalog-20261028T000000000000Z.json"
        try:
            link.symlink_to(self.source)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

        listed = list_catalog_snapshots(history)
        self.assertEqual(listed["snapshots"], [])
        with self.assertRaisesRegex(ValueError, "snapshot must be an existing"):
            restore_catalog_snapshot(link, self.root / "destination.json")

    def test_snapshot_creation_rejects_symlinked_history_directory(self):
        real_history = self.root / "real-history"
        real_history.mkdir()
        linked_history = self.root / "linked-history"
        try:
            linked_history.symlink_to(real_history, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
        with self.assertRaisesRegex(
            ValueError, "history directory must not be a symlink"
        ):
            create_catalog_snapshot(self.source, linked_history)

    def test_credential_material_key_detection_covers_variants_but_allows_env_refs(self):
        for record in (
            {"secret_key": "value"},
            {"provider_api_token": "value"},
            {"auth": {"access_key": "value"}},
            {"clientSecret": "value"},
            {"refresh_token": "value"},
            {"private_key_pem": "value"},
        ):
            with self.subTest(record=record):
                self.assertTrue(_contains_credential_material(record))

        self.assertFalse(
            _contains_credential_material(
                {
                    "api_key_env": "OPENAI_API_KEY",
                    "token_env": "PROVIDER_TOKEN",
                    "credential_env": "PROVIDER_CREDENTIAL",
                }
            )
        )

    def test_export_refuses_malformed_json(self):
        invalid = self.root / "broken.json"
        invalid.write_text("{broken", encoding="utf-8")
        target = self.root / "export.json"
        with self.assertRaises(ValueError):
            export_catalog(invalid, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
