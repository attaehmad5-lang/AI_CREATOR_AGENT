import json
import stat
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.output_validator import ProviderOutputValidator


class ProviderOutputValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.validator = ProviderOutputValidator(max_size_bytes=64)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_image_signature_returns_hash_and_metadata(self):
        path = self.root / "cover.png"
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + struct.pack(">II", 2, 3) + bytes((8, 2, 0, 0, 0))
        )
        result = self.validator.validate_file(path, modality="image")
        self.assertTrue(result["valid"])
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(len(result["sha256"]), 64)

    def test_image_dimensions_are_reported_and_pixel_bombs_rejected(self):
        path = self.root / "large.png"
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big") + b"IHDR"
            + struct.pack(">II", 20, 20) + bytes((8, 2, 0, 0, 0))
        )
        limited = ProviderOutputValidator(max_size_bytes=64, max_image_pixels=100)
        result = limited.validate_file(path, modality="image")
        self.assertEqual(result["status"], "IMAGE_PIXEL_LIMIT_EXCEEDED")
        self.assertEqual(result["width"], 20)
        self.assertEqual(result["height"], 20)

        normal = self.validator.validate_file(path, modality="image")
        self.assertTrue(normal["valid"])
        self.assertEqual(normal["pixel_count"], 400)

    def test_jpeg_gif_and_extended_webp_dimensions_are_supported(self):
        jpeg = self.root / "sample.jpg"
        jpeg.write_bytes(
            b"\\xff\\xd8\\xff\\xc0" + (8).to_bytes(2, "big")
            + bytes((8, 0, 3, 0, 4, 1))
        )
        jpeg_result = self.validator.validate_file(jpeg, modality="image")
        self.assertTrue(jpeg_result["valid"], jpeg_result)
        self.assertEqual((jpeg_result["width"], jpeg_result["height"]), (4, 3))

        gif = self.root / "sample.gif"
        gif.write_bytes(b"GIF89a" + struct.pack("<HH", 5, 6) + b"\\x00" * 20)
        gif_result = self.validator.validate_file(gif, modality="image")
        self.assertTrue(gif_result["valid"], gif_result)
        self.assertEqual((gif_result["width"], gif_result["height"]), (5, 6))

        webp = self.root / "sample.webp"
        webp.write_bytes(
            b"RIFF" + (22).to_bytes(4, "little") + b"WEBPVP8X"
            + (10).to_bytes(4, "little") + b"\\x00\\x00\\x00\\x00"
            + (3).to_bytes(3, "little") + (2).to_bytes(3, "little")
        )
        webp_result = self.validator.validate_file(webp, modality="image")
        self.assertTrue(webp_result["valid"], webp_result)
        self.assertEqual((webp_result["width"], webp_result["height"]), (4, 3))

    def test_image_with_missing_or_zero_dimensions_is_rejected(self):
        path = self.root / "broken.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"short")
        self.assertEqual(
            self.validator.validate_file(path, modality="image")["status"],
            "IMAGE_DIMENSIONS_INVALID",
        )
        zero = self.root / "zero.png"
        zero.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big") + b"IHDR"
            + struct.pack(">II", 0, 10) + bytes((8, 2, 0, 0, 0))
        )
        self.assertEqual(
            self.validator.validate_file(zero, modality="image")["status"],
            "IMAGE_DIMENSIONS_INVALID",
        )

    def test_mismatched_signature_and_extension_are_rejected(self):
        wrong_signature = self.root / "cover.png"
        wrong_signature.write_bytes(b"not an image")
        self.assertEqual(
            self.validator.validate_file(wrong_signature, modality="image")["status"],
            "SIGNATURE_MISMATCH",
        )
        wrong_extension = self.root / "cover.exe"
        wrong_extension.write_bytes(b"\x89PNG\r\n\x1a\n")
        self.assertEqual(
            self.validator.validate_file(wrong_extension, modality="image")["status"],
            "EXTENSION_MISMATCH",
        )

    def test_missing_empty_and_oversized_outputs_are_rejected(self):
        self.assertEqual(
            self.validator.validate_file(self.root / "missing.png", modality="image")["status"],
            "OUTPUT_NOT_FOUND",
        )
        empty = self.root / "empty.txt"
        empty.write_bytes(b"")
        self.assertEqual(
            self.validator.validate_file(empty, modality="text")["status"],
            "OUTPUT_TOO_SMALL",
        )
        large = self.root / "large.txt"
        large.write_bytes(b"x" * 65)
        self.assertEqual(
            self.validator.validate_file(large, modality="text")["status"],
            "OUTPUT_TOO_LARGE",
        )

    def test_text_json_and_pdf_content_are_checked(self):
        text_file = self.root / "script.txt"
        text_file.write_text("hello world", encoding="utf-8")
        self.assertTrue(self.validator.validate_file(text_file, modality="text")["valid"])
        invalid_text = self.root / "bad.txt"
        invalid_text.write_bytes(b"\xff\xfe\xfa")
        self.assertEqual(
            self.validator.validate_file(invalid_text, modality="text")["status"],
            "INVALID_TEXT_ENCODING",
        )
        json_file = self.root / "metadata.json"
        json_file.write_text(json.dumps({"title": "test"}), encoding="utf-8")
        self.assertTrue(self.validator.validate_file(json_file, modality="json")["valid"])
        bad_json = self.root / "bad.json"
        bad_json.write_text("{", encoding="utf-8")
        self.assertEqual(
            self.validator.validate_file(bad_json, modality="json")["status"],
            "INVALID_JSON",
        )
        nonstandard_json = self.root / "nonstandard.json"
        nonstandard_json.write_text('{"amount": NaN}', encoding="utf-8")
        self.assertEqual(
            self.validator.validate_file(nonstandard_json, modality="json")["status"],
            "INVALID_JSON",
        )
        pdf = self.root / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        self.assertEqual(
            self.validator.validate_file(pdf, modality="document")["mime_type"],
            "application/pdf",
        )

    def test_file_growth_during_validation_is_detected(self):
        path = self.root / "changing.png"
        signature = b"\x89PNG\r\n\x1a\n"
        payload = (
            signature + (13).to_bytes(4, "big") + b"IHDR"
            + struct.pack(">II", 2, 3) + bytes((8, 2, 0, 0, 0))
        )
        path.write_bytes(payload)
        actual = path.stat()
        fake_stat = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_size=len(signature),
            st_dev=actual.st_dev,
            st_ino=actual.st_ino,
        )
        with patch("projects.provider_store.output_validator.os.fstat", return_value=fake_stat):
            result = self.validator.validate_file(path, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "OUTPUT_CHANGED_DURING_VALIDATION")

    def test_same_size_file_mutation_is_detected_by_final_metadata(self):
        path = self.root / "changing.txt"
        path.write_text("before", encoding="utf-8")
        actual = path.stat()
        initial = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_size=6,
            st_dev=actual.st_dev, st_ino=actual.st_ino,
            st_mtime_ns=100, st_ctime_ns=100,
        )
        changed = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_size=6,
            st_dev=actual.st_dev, st_ino=actual.st_ino,
            st_mtime_ns=101, st_ctime_ns=101,
        )
        with patch(
            "projects.provider_store.output_validator.os.fstat",
            side_effect=[initial, changed],
        ):
            result = self.validator.validate_file(path, modality="text")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "OUTPUT_CHANGED_DURING_VALIDATION")

    def test_deeply_nested_json_is_rejected_without_crashing(self):
        validator = ProviderOutputValidator(max_size_bytes=10000)
        path = self.root / "deep.json"
        path.write_text("[" * 1500 + "0" + "]" * 1500, encoding="utf-8")
        result = validator.validate_file(path, modality="json")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "JSON_NESTING_LIMIT")

    def test_symlinks_and_unsupported_modalities_fail_closed(self):
        source = self.root / "source.txt"
        source.write_text("safe", encoding="utf-8")
        link = self.root / "linked.txt"
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable in this environment")
        self.assertEqual(
            self.validator.validate_file(link, modality="text")["status"],
            "SYMLINK_REJECTED",
        )
        self.assertEqual(
            self.validator.validate_file(source, modality="archive")["status"],
            "UNSUPPORTED_MODALITY",
        )

    def test_invalid_size_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderOutputValidator(min_size_bytes=0)
        with self.assertRaises(ValueError):
            ProviderOutputValidator(max_size_bytes=0)
        with self.assertRaises(ValueError):
            ProviderOutputValidator(min_size_bytes=10, max_size_bytes=1)
        with self.assertRaises(ValueError):
            ProviderOutputValidator(max_json_depth=0)
        self.assertEqual(
            ProviderOutputValidator().validate_file("bad\x00path.txt", modality="text")["status"],
            "INVALID_PATH",
        )


if __name__ == "__main__":
    unittest.main()
