import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.catalog_cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "projects" / "provider_store" / "provider_catalog.json"


class CatalogCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.current = self.root / "current.json"
        self.current.write_bytes(CATALOG.read_bytes())
        self.candidate = self.root / "candidate.json"
        self.catalog = json.loads(self.current.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([str(arg) for arg in args])
        return code, json.loads(output.getvalue())

    def test_apply_is_dry_run_by_default(self):
        candidate = dict(self.catalog)
        candidate["version"] = "9.9"
        self.candidate.write_text(json.dumps(candidate), encoding="utf-8")
        original = self.current.read_bytes()
        code, result = self.invoke("apply", self.current, self.candidate)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "CATALOG_DIFF_DRY_RUN")
        self.assertEqual(self.current.read_bytes(), original)

    def test_removal_needs_explicit_approval(self):
        candidate = json.loads(json.dumps(self.catalog))
        candidate["providers"].pop()
        self.candidate.write_text(json.dumps(candidate), encoding="utf-8")
        original = self.current.read_bytes()
        code, result = self.invoke("apply", self.current, self.candidate, "--apply")
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED")
        self.assertEqual(self.current.read_bytes(), original)

    def test_snapshot_history_and_restore_use_safe_output(self):
        history = self.root / "history"
        code, created = self.invoke("snapshot", self.current, history)
        self.assertEqual(code, 0)
        snapshot = history / created["snapshot"]
        self.assertTrue(snapshot.is_file())
        code, listed = self.invoke("history", history)
        self.assertEqual(code, 0)
        self.assertEqual(listed["snapshots"][0]["name"], snapshot.name)
        self.assertNotIn("providers", json.dumps(listed))
        altered = json.loads(self.current.read_text(encoding="utf-8"))
        altered["version"] = "9.9"
        self.current.write_text(json.dumps(altered), encoding="utf-8")
        code, restored = self.invoke("restore", snapshot, self.current)
        self.assertEqual(code, 0)
        self.assertTrue(restored["restored"])
        self.assertTrue(Path(str(self.current) + ".bak").is_file())
        self.assertEqual(json.loads(self.current.read_text(encoding="utf-8"))["version"], self.catalog["version"])

    def test_prune_command_is_preview_only_until_apply_is_explicit(self):
        history = self.root / "history"
        history.mkdir()
        for day in range(1, 4):
            (history / f"catalog-2026010{day}T000000000000Z.json").write_bytes(
                self.current.read_bytes()
            )
        original = sorted(path.name for path in history.glob("catalog-*.json"))

        code, preview = self.invoke("prune", history, "--keep", "1")
        self.assertEqual(code, 0)
        self.assertFalse(preview["applied"])
        self.assertEqual(len(preview["would_delete"]), 2)
        self.assertEqual(sorted(path.name for path in history.glob("catalog-*.json")), original)

        code, applied = self.invoke("prune", history, "--keep", "1", "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(applied["applied"])
        self.assertEqual(len(applied["deleted"]), 2)
        self.assertEqual(len(list(history.glob("catalog-*.json"))), 1)

    def test_export_command_creates_valid_catalog(self):
        destination = self.root / "exported" / "catalog.json"
        code, result = self.invoke("export", self.current, destination)
        self.assertEqual(code, 0)
        self.assertTrue(result["exported"])
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), self.catalog)


if __name__ == "__main__":
    unittest.main()
