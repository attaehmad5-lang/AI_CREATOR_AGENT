import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from projects.provider_store.advanced.release_report import build_release_report, main, read_ledger, write_release_artifacts


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

    def test_release_writer_rejects_symlink_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                _ledger_text(), encoding="utf-8"
            )
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            output = root / "release-output"
            try:
                output.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            with self.assertRaisesRegex(ValueError, "output directory must not be a symlink"):
                write_release_artifacts(output, repo_root=root)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(list(outside.iterdir()), [marker])

    def test_release_writer_rejects_symlinked_artifact_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                _ledger_text(), encoding="utf-8"
            )
            output = root / "release-output"
            output.mkdir()
            outside = root / "outside.txt"
            outside.write_text("preserve", encoding="utf-8")
            target = output / "advanced_provider_store_report.json"
            try:
                target.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            with self.assertRaisesRegex(ValueError, "artifact destination must not be a symlink"):
                write_release_artifacts(output, repo_root=root)
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")

    def test_ledger_rejects_symlink_without_reading_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.md"
            outside.write_text(_ledger_text(), encoding="utf-8")
            ledger = root / "ledger.md"
            try:
                ledger.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                read_ledger(ledger)
            self.assertTrue(ledger.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), _ledger_text())

    def test_require_complete_cli_blocks_incomplete_release(self):
        from unittest.mock import patch

        incomplete = {
            "completion_status": "INCOMPLETE",
            "zip_integrity": "PASS",
            "points_completed": 88,
            "points_total": 150,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch("projects.provider_store.advanced.release_report.write_release_artifacts", return_value=incomplete):
                with patch("sys.argv", ["release_report", "--output-dir", tmp, "--require-complete"]):
                    self.assertEqual(main(), 2)

    def test_incomplete_report_remains_valid_without_release_gate_flag(self):
        from unittest.mock import patch

        incomplete = {
            "completion_status": "INCOMPLETE",
            "zip_integrity": "PASS",
            "points_completed": 88,
            "points_total": 150,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch("projects.provider_store.advanced.release_report.write_release_artifacts", return_value=incomplete):
                with patch("sys.argv", ["release_report", "--output-dir", tmp]):
                    self.assertEqual(main(), 0)

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
            ("google-key.py", "TOKEN = \"AIza" + "D" * 35 + "\"\\n"),
            ("huggingface-token.py", "TOKEN = \"hf_" + "E" * 28 + "\"\\n"),
            ("replicate-token.py", "TOKEN = \"r8_" + "F" * 28 + "\"\\n"),
            ("anthropic-token.py", "TOKEN = \"sk-ant-" + "G" * 28 + "\"\\n"),
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
                with self.assertRaisesRegex(ValueError, "detected in release payload"):
                    write_release_artifacts(root / "build", repo_root=root)

    def test_source_package_fails_closed_on_high_entropy_credential_assignment(self):
        random_value = "Q7mN2xV9pL4kR8tY3wA6cD1fH5jK0sB7"
        fixtures = (
            ("api_key", "api_key"),
            ("private_key_pem", "private_key_pem"),
            ("api_secret", "api_secret"),
            ("provider_api_token", "provider_api_token"),
            ("credentials", "credentials"),
            ("refresh_token", "refresh_token"),
            ("bearer_token", "bearer_token"),
            ("api_key_env", "api_key_env"),
        )
        for filename, key in fixtures:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                provider_root = root / "projects" / "provider_store"
                advanced = provider_root / "advanced"
                advanced.mkdir(parents=True)
                (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                    _ledger_text(), encoding="utf-8"
                )
                (provider_root / f"{filename}.py").write_text(
                    f'{key} = "{random_value}"\\n', encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    ValueError, "high-entropy credential-like value"
                ):
                    write_release_artifacts(root / "build", repo_root=root)

    def test_final_archive_scan_rejects_secret_in_generated_pdf(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_root = root / "projects" / "provider_store"
            advanced = provider_root / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                _ledger_text(), encoding="utf-8"
            )
            (provider_root / "safe.py").write_text("VALUE = 1\\n", encoding="utf-8")

            def write_tainted_pdf(path, report):
                path.write_bytes(b"%PDF-1.4\\nTOKEN=ghp_" + b"A" * 36 + b"\\n%%EOF\\n")

            with patch(
                "projects.provider_store.advanced.pdf_report.write_pdf_report",
                side_effect=write_tainted_pdf,
            ):
                with self.assertRaisesRegex(ValueError, "GitHub access token detected in release payload"):
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
            (provider_root / "safe_credentials.py").write_text(
                "credentials = self.store.manager.credential_status(provider_id)\n",
                encoding="utf-8",
            )
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
