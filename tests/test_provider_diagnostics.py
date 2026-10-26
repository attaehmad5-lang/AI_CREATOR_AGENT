import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.provider_diagnostics import ProviderDiagnostics


class ProviderDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.diagnostics = ProviderDiagnostics()

    def test_valid_json_reports_only_metadata_not_contents(self):
        path = self.root / "private-state.json"
        path.write_text(json.dumps({"api_key": "DO_NOT_LEAK_THIS"}), encoding="utf-8")
        result = self.diagnostics.inspect_files({"connection": path})
        self.assertEqual(result["status"], "HEALTHY")
        self.assertTrue(result["ready"])
        self.assertEqual(result["checks"][0]["status"], "VALID_JSON")
        self.assertNotIn("DO_NOT_LEAK_THIS", repr(result))
        self.assertNotIn("api_key", repr(result))

    def test_missing_malformed_and_scalar_root_fail_closed(self):
        missing = self.root / "missing.json"
        malformed = self.root / "malformed.json"
        scalar = self.root / "scalar.json"
        malformed.write_text("{broken", encoding="utf-8")
        scalar.write_text('"scalar"', encoding="utf-8")
        result = self.diagnostics.inspect_files({
            "missing": missing,
            "malformed": malformed,
            "scalar": scalar,
        })
        self.assertFalse(result["ready"])
        self.assertEqual(result["issue_count"], 3)
        self.assertEqual(
            [item["status"] for item in result["checks"]],
            ["MISSING", "INVALID_JSON", "INVALID_ROOT"],
        )

    def test_duplicate_keys_and_non_finite_numbers_are_rejected(self):
        duplicate = self.root / "duplicate.json"
        non_finite = self.root / "non-finite.json"
        duplicate.write_text('{"status": "HEALTHY", "status": "BLOCKED"}', encoding="utf-8")
        non_finite.write_text('{"cost": NaN}', encoding="utf-8")
        result = self.diagnostics.inspect_files({"duplicate": duplicate, "nonfinite": non_finite})
        self.assertFalse(result["ready"])
        self.assertEqual(result["issue_count"], 2)
        self.assertEqual([item["status"] for item in result["checks"]],
                         ["INVALID_JSON", "INVALID_JSON"])

    def test_oversized_file_is_rejected_before_parsing(self):
        path = self.root / "large.json"
        path.write_text('{"ok": true}', encoding="utf-8")
        result = self.diagnostics.inspect_files({"state": path}, max_file_bytes=4)
        self.assertEqual(result["checks"][0]["status"], "TOO_LARGE")
        self.assertFalse(result["ready"])

    def test_actual_read_is_bounded_if_file_grows_after_stat(self):
        path = self.root / "state.json"
        path.write_text("{}", encoding="utf-8")
        original_open = Path.open

        def growing_open(target, mode="r", *args, **kwargs):
            if target == path and mode == "rb":
                return io.BytesIO(b'{"payload":"' + b"x" * 64 + b'"}')
            return original_open(target, mode, *args, **kwargs)

        with patch.object(Path, "open", growing_open):
            result = self.diagnostics.inspect_files({"state": path}, max_file_bytes=16)

        self.assertFalse(result["ready"])
        self.assertEqual(result["checks"][0]["status"], "TOO_LARGE")
        self.assertLessEqual(result["checks"][0]["bytes"], 17)

    def test_duplicate_paths_are_flagged(self):
        path = self.root / "state.json"
        path.write_text("{}", encoding="utf-8")
        result = self.diagnostics.inspect_files({"first": path, "second": path})
        self.assertEqual(result["checks"][1]["status"], "DUPLICATE_PATH")
        self.assertFalse(result["ready"])

    def test_invalid_inputs_fail_closed(self):
        for files, limit in (({}, 1024), ({"bad/name": "x.json"}, 1024),
                             ({"state": "x.json"}, True), ({"state": "x.json"}, 0)):
            result = self.diagnostics.inspect_files(files, max_file_bytes=limit)
            self.assertEqual(result["status"], "INVALID_DIAGNOSTIC_INPUT")
            self.assertFalse(result["ready"])

    def test_directory_is_not_accepted_as_state_file(self):
        result = self.diagnostics.inspect_files({"state": self.root})
        self.assertEqual(result["checks"][0]["status"], "NOT_A_FILE")
        self.assertFalse(result["ready"])


if __name__ == "__main__":
    unittest.main()
