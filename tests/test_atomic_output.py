import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.atomic_output import publish_validated_output
from projects.provider_store.output_validator import ProviderOutputValidator


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image-data"


class AtomicOutputPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.validator = ProviderOutputValidator(max_size_bytes=1024)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_temp(self, name="candidate.png", payload=PNG):
        path = self.root / name
        path.write_bytes(payload)
        return path

    def test_valid_candidate_atomically_replaces_previous_output(self):
        temporary = self._write_temp()
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        result = publish_validated_output(
            temporary, destination, modality="image", validator=self.validator
        )
        self.assertTrue(result["published"])
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(destination.read_bytes(), PNG)
        self.assertFalse(temporary.exists())
        self.assertEqual(result["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_invalid_candidate_preserves_previous_output_and_cleans_temp(self):
        temporary = self._write_temp(payload=b"not-a-png")
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        result = publish_validated_output(
            temporary, destination, modality="image", validator=self.validator
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "TEMP_OUTPUT_INVALID")
        self.assertEqual(result["validation_status"], "SIGNATURE_MISMATCH")
        self.assertEqual(destination.read_bytes(), b"previous-good-output")
        self.assertFalse(temporary.exists())
        self.assertTrue(result["cleanup_succeeded"])

    def test_missing_candidate_does_not_damage_destination(self):
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        result = publish_validated_output(
            self.root / "missing.png", destination,
            modality="image", validator=self.validator,
        )
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "TEMP_OUTPUT_NOT_FOUND")
        self.assertEqual(destination.read_bytes(), b"previous-good-output")

    def test_candidate_must_be_in_same_directory_for_atomic_replace(self):
        other = Path(tempfile.mkdtemp())
        try:
            temporary = other / "candidate.png"
            temporary.write_bytes(PNG)
            destination = self.root / "final.png"
            destination.write_bytes(b"previous-good-output")
            result = publish_validated_output(
                temporary, destination, modality="image", validator=self.validator
            )
            self.assertEqual(result["status"], "TEMP_FILE_MUST_SHARE_DESTINATION_DIRECTORY")
            self.assertEqual(destination.read_bytes(), b"previous-good-output")
            self.assertFalse(temporary.exists())
        finally:
            other.rmdir()

    def test_path_collision_never_deletes_destination(self):
        destination = self.root / "final.png"
        destination.write_bytes(PNG)
        result = publish_validated_output(
            destination, destination, modality="image", validator=self.validator
        )
        self.assertEqual(result["status"], "PATH_COLLISION")
        self.assertEqual(destination.read_bytes(), PNG)

    def test_destination_symlink_is_rejected_without_touching_target(self):
        target = self.root / "target.png"
        target.write_bytes(b"original-target")
        destination = self.root / "final.png"
        try:
            destination.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable in this environment")
        temporary = self._write_temp()
        result = publish_validated_output(
            temporary, destination, modality="image", validator=self.validator
        )
        self.assertEqual(result["status"], "DESTINATION_SYMLINK_REJECTED")
        self.assertEqual(target.read_bytes(), b"original-target")
        self.assertTrue(destination.is_symlink())
        self.assertFalse(temporary.exists())

    def test_hash_mismatch_detects_candidate_mutation_and_preserves_old_output(self):
        class WrongHashValidator:
            def validate_file(self, path, *, modality):
                return {"valid": True, "sha256": "0" * 64, "size_bytes": path.stat().st_size}

        temporary = self._write_temp()
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        result = publish_validated_output(
            temporary, destination, modality="image", validator=WrongHashValidator()
        )
        self.assertEqual(result["status"], "TEMP_OUTPUT_CHANGED")
        self.assertEqual(destination.read_bytes(), b"previous-good-output")
        self.assertFalse(temporary.exists())

    def test_validator_exception_preserves_destination_and_cleans_candidate(self):
        class BrokenValidator:
            def validate_file(self, path, *, modality):
                raise RuntimeError("private internal detail")

        temporary = self._write_temp()
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        result = publish_validated_output(
            temporary, destination, modality="image", validator=BrokenValidator()
        )
        self.assertEqual(result["status"], "TEMP_OUTPUT_VALIDATION_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(destination.read_bytes(), b"previous-good-output")
        self.assertFalse(temporary.exists())
        self.assertNotIn("private internal detail", repr(result))

    def test_atomic_replace_failure_preserves_destination_and_cleans_candidate(self):
        temporary = self._write_temp()
        destination = self.root / "final.png"
        destination.write_bytes(b"previous-good-output")
        with patch("projects.provider_store.atomic_output.os.replace", side_effect=PermissionError):
            result = publish_validated_output(
                temporary, destination, modality="image", validator=self.validator
            )
        self.assertEqual(result["status"], "ATOMIC_PUBLISH_FAILED")
        self.assertEqual(result["error_type"], "PermissionError")
        self.assertEqual(destination.read_bytes(), b"previous-good-output")
        self.assertFalse(temporary.exists())
        self.assertTrue(result["cleanup_succeeded"])

    def test_invalid_path_and_missing_validator_fail_closed(self):
        self.assertEqual(
            publish_validated_output(
                "bad\x00path.png", self.root / "final.png",
                modality="image", validator=self.validator,
            )["status"],
            "INVALID_PATH",
        )
        temporary = self._write_temp()
        result = publish_validated_output(
            temporary, self.root / "final.png", modality="image", validator=None
        )
        self.assertEqual(result["status"], "VALIDATOR_UNAVAILABLE")
        self.assertTrue(temporary.exists())


if __name__ == "__main__":
    unittest.main()
