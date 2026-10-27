import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.catalog_diff import apply_catalog_candidate, diff_catalogs


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "projects" / "provider_store" / "provider_catalog.json"


class CatalogDiffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.current = self.root / "current.json"
        self.candidate = self.root / "candidate.json"
        self.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.current.write_text(json.dumps(self.catalog), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def write_candidate(self, catalog):
        self.candidate.write_text(json.dumps(catalog), encoding="utf-8")

    def test_diff_reports_added_removed_changed_fields_without_values(self):
        candidate = json.loads(json.dumps(self.catalog))
        first = candidate["providers"][0]
        first["name"] = first.get("name", "provider") + " changed"
        removed_id = candidate["providers"].pop()["id"]
        new_provider = json.loads(json.dumps(candidate["providers"][0]))
        new_provider["id"] = "new-test-provider"
        new_provider["name"] = "New Test Provider"
        candidate["providers"].append(new_provider)
        self.write_candidate(candidate)

        result = diff_catalogs(self.current, self.candidate)
        self.assertEqual(result["status"], "CATALOG_DIFF_READY")
        self.assertIn("new-test-provider", result["added"])
        self.assertIn(removed_id, result["removed"])
        self.assertIn("name", result["changed"][0]["changed_fields"])
        self.assertNotIn("New Test Provider", json.dumps(result))

    def test_dry_run_is_default_and_does_not_mutate_current_catalog(self):
        candidate = json.loads(json.dumps(self.catalog))
        candidate["version"] = "9.9"
        self.write_candidate(candidate)
        original = self.current.read_bytes()

        result = apply_catalog_candidate(self.current, self.candidate)
        self.assertEqual(result["status"], "CATALOG_DIFF_DRY_RUN")
        self.assertFalse(result["applied"])
        self.assertEqual(self.current.read_bytes(), original)

    def test_removal_requires_explicit_approval_and_apply_keeps_backup(self):
        candidate = json.loads(json.dumps(self.catalog))
        candidate["providers"].pop()
        self.write_candidate(candidate)
        original = self.current.read_bytes()

        blocked = apply_catalog_candidate(self.current, self.candidate, dry_run=False)
        self.assertEqual(blocked["status"], "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED")
        self.assertEqual(self.current.read_bytes(), original)

        applied = apply_catalog_candidate(
            self.current, self.candidate, dry_run=False, allow_removals=True
        )
        self.assertTrue(applied["applied"])
        self.assertTrue(Path(str(self.current) + ".bak").is_file())
        self.assertEqual(
            len(json.loads(self.current.read_text(encoding="utf-8"))["providers"]),
            len(candidate["providers"]),
        )

    def test_invalid_boolean_controls_are_rejected(self):
        with self.assertRaises(ValueError):
            apply_catalog_candidate(self.current, self.candidate, dry_run="no")


if __name__ == "__main__":
    unittest.main()
