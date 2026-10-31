import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.advanced.pdf_report import write_pdf_report


class PdfReportTests(unittest.TestCase):
    def test_generates_valid_pdf_header_and_feature_summary(self):
        report = {
            "completion_status": "INCOMPLETE",
            "points_completed": 3,
            "points_total": 150,
            "points_remaining": 147,
            "release_ready": False,
            "live_provider_verification": "NOT_CLAIMED",
            "status_counts": {"COMPLETED": 3, "PARTIAL": 7, "UNVERIFIED": 140},
            "ledger": [{
                "id": 1,
                "feature": "Provider Catalog Management",
                "status": "PARTIAL",
                "evidence": "Catalog and validation tests exist",
                "remaining": "Finish runtime adapter verification",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_pdf_report(Path(directory) / "report.pdf", report)
            data = path.read_bytes()
            self.assertTrue(path.is_file())
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertGreater(len(data), 500)
        self.assertIn(b"startxref", data)
        self.assertIn(b"Provider Catalog Management", data)

    def test_preserves_winansi_text_and_escapes_pdf_delimiters(self):
        report = {
            "completion_status": "INCOMPLETE",
            "points_completed": 0,
            "points_total": 150,
            "points_remaining": 150,
            "release_ready": False,
            "live_provider_verification": "NOT_CLAIMED",
            "status_counts": {},
            "ledger": [{
                "id": 1,
                "feature": "Café (vision) \\ model",
                "status": "PARTIAL",
                "evidence": "Résumé – naïve",
                "remaining": "Check emoji 🚀 fallback",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            data = write_pdf_report(Path(directory) / "unicode.pdf", report).read_bytes()
        self.assertIn(b"/Encoding /WinAnsiEncoding", data)
        self.assertIn(b"Caf\\351", data)
        self.assertIn(b"\\(vision\\)", data)
        self.assertIn(b"R\\351sum\\351", data)
        self.assertIn(b"?", data)
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))

    def test_empty_ledger_still_produces_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_pdf_report(Path(directory) / "empty.pdf", {})
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"%PDF-1.4"))


    def test_rejects_symlink_destination_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "protected.pdf"
            original = b"keep-original-pdf"
            target.write_bytes(original)
            link = root / "report.pdf"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")

            with self.assertRaisesRegex(ValueError, "destination must not be a symlink"):
                write_pdf_report(link, {})
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), original)

    def test_atomic_replace_failure_preserves_existing_pdf_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "report.pdf"
            original = b"existing-report"
            target.write_bytes(original)
            with patch(
                "projects.provider_store.advanced.pdf_report.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    write_pdf_report(target, {})
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.glob(".report.pdf.*.tmp")), [])


    def test_large_feature_ledger_is_split_across_multiple_pdf_pages(self):
        report = {
            "completion_status": "INCOMPLETE",
            "points_completed": 0,
            "points_total": 150,
            "points_remaining": 150,
            "release_ready": False,
            "live_provider_verification": "NOT_CLAIMED",
            "status_counts": {"NOT_STARTED": 150},
            "ledger": [
                {"id": index, "feature": f"Feature {index:03d}",
                 "status": "NOT_STARTED", "evidence": "No evidence yet",
                 "remaining": "Implementation and tests required"}
                for index in range(1, 151)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            data = write_pdf_report(Path(directory) / "large.pdf", report).read_bytes()

        self.assertIn(b"/Count 7", data)
        self.assertIn(b"Feature 001", data)
        self.assertIn(b"Feature 150", data)
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))



if __name__ == "__main__":
    unittest.main()
