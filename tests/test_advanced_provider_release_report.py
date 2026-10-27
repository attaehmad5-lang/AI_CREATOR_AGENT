import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from projects.provider_store.advanced.release_report import build_release_report, read_ledger, write_release_artifacts


def _ledger_text(count=150):
    lines = [
        "# Ledger",
        "| # | Feature | Status | Evidence | Remaining work |",
        "|---:|---|---|---|---|",
    ]
    for number in range(1, count + 1):
        status = "COMPLETED" if number <= 3 else "PARTIAL"
        lines.append(f"| {number:03d} | Feature {number} | {status} | test evidence | continue audit |")
    return "\n".join(lines) + "\n"


class AdvancedProviderReleaseReportTests(unittest.TestCase):
    def test_report_counts_150_points_without_claiming_release_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.md"
            ledger.write_text(_ledger_text(), encoding="utf-8")
            report = build_release_report(ledger)
            self.assertEqual(report["points_total"], 150)
            self.assertEqual(report["points_completed"], 3)
            self.assertEqual(report["points_remaining"], 147)
            self.assertEqual(report["completion_status"], "INCOMPLETE")
            self.assertFalse(report["release_ready"])
            self.assertEqual(report["live_provider_verification"], "NOT_CLAIMED")

    def test_ledger_rejects_missing_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.md"
            ledger.write_text(_ledger_text(149), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "each point ID"):
                read_ledger(ledger)
            ledger.write_text(_ledger_text().replace("| 150 | Feature 150", "| 149 | Feature 150"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "each point ID"):
                read_ledger(ledger)

    def test_source_zip_has_manifest_hashes_and_excludes_runtime_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(_ledger_text(), encoding="utf-8")
            (provider_root / "provider_status_manager.py").write_text("class StatusManager: pass\n", encoding="utf-8")
            (provider_root / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
            (provider_root / "advanced_provider_catalog_backup.json").write_text('{"api_key":"never-package"}', encoding="utf-8")
            (provider_root / ".env").write_text("API_KEY=never-package", encoding="utf-8")
            output = root / "build"
            result = write_release_artifacts(output, repo_root=root)
            self.assertEqual(result["zip_integrity"], "PASS")
            self.assertEqual(result["completion_status"], "INCOMPLETE")
            self.assertGreaterEqual(result["files_packaged"], 3)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("projects/provider_store/provider_status_manager.py", paths)
            self.assertNotIn("projects/provider_store/advanced_provider_catalog_backup.json", paths)
            self.assertNotIn("projects/provider_store/.env", paths)
            with zipfile.ZipFile(result["archive"]) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertNotIn("projects/provider_store/.env", names)
                self.assertNotIn("projects/provider_store/advanced_provider_catalog_backup.json", names)
                self.assertIn("RELEASE_REPORT.json", names)
                self.assertIn("SOURCE_MANIFEST.json", names)
                for entry in manifest["files"]:
                    digest = hashlib.sha256(archive.read(entry["path"])).hexdigest()
                    self.assertEqual(digest, entry["sha256"])


if __name__ == "__main__":
    unittest.main()
