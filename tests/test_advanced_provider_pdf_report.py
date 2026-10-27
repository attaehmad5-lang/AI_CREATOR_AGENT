import tempfile
import unittest
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

    def test_empty_ledger_still_produces_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_pdf_report(Path(directory) / "empty.pdf", {})
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_bytes().startswith(b"%PDF-1.4"))


if __name__ == "__main__":
    unittest.main()
