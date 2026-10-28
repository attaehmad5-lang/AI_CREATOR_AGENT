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

    def test_source_package_fails_closed_on_private_key_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(_ledger_text(), encoding="utf-8")
            (provider_root / "unsafe_key.md").write_text("-----BEGIN PRIVATE KEY-----\\nnot-a-real-key\\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "private-key block"):
                write_release_artifacts(root / "build", repo_root=root)

    def test_source_package_fails_closed_on_high_confidence_provider_tokens(self):
        fixtures = (
            ("github-token.py", "TOKEN = \"ghp_" + "A" * 36 + "\"\\n"),
            ("api-token.py", "TOKEN = \"sk-" + "B" * 32 + "\"\\n"),
            ("aws-key.py", "TOKEN = \"AKIA" + "A" * 16 + "\"\\n"),
            ("slack-token.py", "TOKEN = \"xoxb-" + "C" * 28 + "\"\\n"),
        )
        for filename, contents in fixtures:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                provider_root = root / "projects" / "provider_store"
                advanced = provider_root / "advanced"
                advanced.mkdir(parents=True)
                (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                    _ledger_text(), encoding="utf-8"
                )
                (provider_root / filename).write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "detected in eligible source file"):
                    write_release_artifacts(root / "build", repo_root=root)

    def test_source_zip_has_manifest_hashes_and_excludes_runtime_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[build-system]\nrequires = []\n", encoding="utf-8")
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(_ledger_text(), encoding="utf-8")
            (provider_root / "provider_status_manager.py").write_text("class StatusManager: pass\n", encoding="utf-8")
            (provider_root / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
            (provider_root / "advanced_provider_catalog_backup.json").write_text('{"api_key":"never-package"}', encoding="utf-8")
            (provider_root / "connection_state.json").write_text('{"providers":{}}', encoding="utf-8")
            (provider_root / ".env").write_text("API_KEY=never-package", encoding="utf-8")
            output = root / "build"
            result = write_release_artifacts(output, repo_root=root)
            self.assertEqual(result["zip_integrity"], "PASS")
            self.assertEqual(result["completion_status"], "INCOMPLETE")
            pdf_path = Path(result["report_pdf"])
            self.assertTrue(pdf_path.is_file())
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-1.4"))
            self.assertGreaterEqual(result["files_packaged"], 3)
            checksum_text = Path(result["archive_checksum"]).read_text(encoding="ascii").split()
            self.assertEqual(checksum_text[0], hashlib.sha256(Path(result["archive"]).read_bytes()).hexdigest())
            self.assertEqual(checksum_text[1], Path(result["archive"]).name)
            manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
            sbom = json.loads(Path(result["sbom"]).read_text(encoding="utf-8"))
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            self.assertEqual(len(sbom["files"]), manifest["file_count"])
            sbom_paths = {entry["fileName"] for entry in sbom["files"]}
            self.assertEqual(sbom_paths, {entry["path"] for entry in manifest["files"]})
            self.assertTrue(all(entry["checksums"][0]["algorithm"] == "SHA256" for entry in sbom["files"]))
            paths = {entry["path"] for entry in manifest["files"]}
            self.assertIn("projects/provider_store/provider_status_manager.py", paths)
            self.assertIn("pyproject.toml", paths)
            self.assertNotIn("projects/provider_store/advanced_provider_catalog_backup.json", paths)
            self.assertNotIn("projects/provider_store/connection_state.json", paths)
            self.assertNotIn("projects/provider_store/.env", paths)
            with zipfile.ZipFile(result["archive"]) as archive:
                self.assertIsNone(archive.testzip())
                names = set(archive.namelist())
                self.assertNotIn("projects/provider_store/.env", names)
                self.assertNotIn("projects/provider_store/advanced_provider_catalog_backup.json", names)
                self.assertNotIn("projects/provider_store/connection_state.json", names)
                self.assertIn("RELEASE_REPORT.json", names)
                self.assertIn("SOURCE_MANIFEST.json", names)
                self.assertIn("pyproject.toml", names)
                self.assertIn("SBOM.spdx.json", names)
                self.assertEqual(json.loads(archive.read("SBOM.spdx.json"))["spdxVersion"], "SPDX-2.3")
                self.assertIn("ADVANCED_PROVIDER_STORE_REPORT.pdf", names)
                self.assertTrue(archive.read("ADVANCED_PROVIDER_STORE_REPORT.pdf").startswith(b"%PDF-1.4"))
                for entry in manifest["files"]:
                    digest = hashlib.sha256(archive.read(entry["path"])).hexdigest()
                    self.assertEqual(digest, entry["sha256"])
                with tempfile.TemporaryDirectory() as extracted:
                    archive.extractall(extracted)
                    extracted_root = Path(extracted)
                    self.assertTrue((extracted_root / "RELEASE_REPORT.json").is_file())
                    self.assertEqual(
                        (extracted_root / "projects/provider_store/example.py").read_text(encoding="utf-8"),
                        "VALUE = 1\n",
                    )


if __name__ == "__main__":
    unittest.main()
