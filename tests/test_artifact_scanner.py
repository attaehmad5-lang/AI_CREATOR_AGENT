import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from projects.provider_store.artifact_scanner import ClamAVArtifactScanner


class ClamAVArtifactScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifact = Path(self.temp_dir.name) / "artifact.bin"
        self.artifact.write_bytes(b"untrusted artifact bytes")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_clean_verdict_is_sanitized(self):
        calls = []
        scanner = ClamAVArtifactScanner(
            runner=lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(
                returncode=0, stdout="secret path details", stderr=""
            )
        )
        result = scanner.scan(self.artifact, modality="image")
        self.assertEqual(result, {"clean": True, "status": "CLEAN"})
        self.assertEqual(calls[0][0][0][0], "clamscan")
        self.assertNotIn("secret path details", repr(result))

    def test_infected_verdict_does_not_echo_scanner_output(self):
        scanner = ClamAVArtifactScanner(
            runner=lambda *args, **kwargs: SimpleNamespace(
                returncode=1, stdout="MALWARE /private/path token=secret", stderr=""
            )
        )
        result = scanner.scan(self.artifact, modality="image")
        self.assertEqual(result, {"clean": False, "status": "MALWARE_DETECTED"})
        self.assertNotIn("private/path", repr(result))
        self.assertNotIn("secret", repr(result))

    def test_missing_scanner_and_timeout_fail_closed(self):
        missing = ClamAVArtifactScanner(runner=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(missing.scan(self.artifact, modality="image")["status"], "SCANNER_UNAVAILABLE")
        timeout = ClamAVArtifactScanner(
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("clamscan", 1))
        )
        self.assertEqual(timeout.scan(self.artifact, modality="image")["status"], "SCAN_TIMEOUT")

    def test_size_and_path_validation_fail_closed(self):
        oversized = ClamAVArtifactScanner(max_file_size_bytes=2, runner=lambda *args, **kwargs: self.fail("must not run"))
        self.assertEqual(oversized.scan(self.artifact, modality="image")["status"], "ARTIFACT_TOO_LARGE")
        self.assertEqual(oversized.scan("\x00bad", modality="image")["status"], "INVALID_PATH")

    def test_scanner_configuration_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            ClamAVArtifactScanner(timeout_seconds=float("inf"))
        with self.assertRaises(ValueError):
            ClamAVArtifactScanner(max_file_size_bytes=0)


if __name__ == "__main__":
    unittest.main()
