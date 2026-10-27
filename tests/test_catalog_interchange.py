import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.catalog_interchange import export_catalog, import_catalog


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

    def test_invalid_source_does_not_mutate_destination(self):
        invalid = self.root / "invalid.json"
        invalid.write_text(
            json.dumps(
                {
                    "version": "3.0",
                    "providers": [{"id": "x", "api_key": "never-export"}],
                }
            ),
            encoding="utf-8",
        )
        target = self.root / "target.json"
        original = b'{"version":"3.0","providers":[]}\n'
        target.write_bytes(original)
        with self.assertRaises(ValueError):
            import_catalog(invalid, target)
        self.assertEqual(target.read_bytes(), original)

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

    def test_export_refuses_malformed_json(self):
        invalid = self.root / "broken.json"
        invalid.write_text("{broken", encoding="utf-8")
        target = self.root / "export.json"
        with self.assertRaises(ValueError):
            export_catalog(invalid, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
