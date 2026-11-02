import json
import stat
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
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        result = self.validator.validate_file(path, modality="image")
        self.assertTrue(result["valid"])
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(len(result["sha256"]), 64)

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
        path.write_bytes(signature + b"extra bytes")
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


    @staticmethod
    def _png_header(width, height):
        ihdr = (
            width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes((8, 2, 0, 0, 0))
        )
        return (
            b"\\x89PNG\\r\\n\\x1a\\n" + (13).to_bytes(4, "big")
            + b"IHDR" + ihdr + b"\\x00\\x00\\x00\\x00"
        )

    def test_png_dimensions_are_reported_and_limits_are_enforced(self):
        valid = self.root / "bounded.png"
        valid.write_bytes(self._png_header(640, 480))
        validator = ProviderOutputValidator(
            max_size_bytes=128, max_image_dimension=1000, max_image_pixels=500_000,
        )
        result = validator.validate_file(valid, modality="image")
        self.assertTrue(result["valid"])
        self.assertTrue(result["image_dimensions_validated"])
        self.assertEqual((result["width"], result["height"]), (640, 480))

        too_large = self.root / "oversized.png"
        too_large.write_bytes(self._png_header(1001, 1))
        result = validator.validate_file(too_large, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "IMAGE_DIMENSIONS_EXCEEDED")

        too_many_pixels = self.root / "too-many-pixels.png"
        too_many_pixels.write_bytes(self._png_header(800, 800))
        result = validator.validate_file(too_many_pixels, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "IMAGE_DIMENSIONS_EXCEEDED")

    def test_zero_png_dimensions_are_rejected(self):
        path = self.root / "zero-dimensions.png"
        path.write_bytes(self._png_header(0, 100))
        result = ProviderOutputValidator(max_size_bytes=128).validate_file(path, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "INVALID_IMAGE_DIMENSIONS")

    def test_jpeg_gif_and_webp_headers_report_dimensions(self):
        jpeg = self.root / "dimensions.jpg"
        jpeg.write_bytes(
            b"\\xff\\xd8\\xff\\xc0" + (17).to_bytes(2, "big")
            + b"\\x08" + (100).to_bytes(2, "big") + (200).to_bytes(2, "big")
            + b"\\x03\\x01\\x11\\x00\\x02\\x11\\x00\\x03\\x11\\x00"
            + b"\\xff\\xd9"
        )
        gif = self.root / "dimensions.gif"
        gif.write_bytes(b"GIF89a" + (320).to_bytes(2, "little") + (200).to_bytes(2, "little"))
        webp = self.root / "dimensions.webp"
        vp8x = (
            b"\\x00\\x00\\x00\\x00" + (319).to_bytes(3, "little")
            + (239).to_bytes(3, "little")
        )
        webp.write_bytes(
            b"RIFF" + (22).to_bytes(4, "little") + b"WEBP"
            + b"VP8X" + (10).to_bytes(4, "little") + vp8x
        )
        validator = ProviderOutputValidator(max_size_bytes=128)
        expected = ((jpeg, 200, 100), (gif, 320, 200), (webp, 320, 240))
        for path, width, height in expected:
            with self.subTest(path=path.name):
                result = validator.validate_file(path, modality="image")
                self.assertTrue(result["valid"], result)
                self.assertTrue(result["image_dimensions_validated"])
                self.assertEqual((result["width"], result["height"]), (width, height))

    def test_legacy_signature_only_images_report_dimensions_unavailable(self):
        path = self.root / "legacy.png"
        path.write_bytes(b"\\x89PNG\\r\\n\\x1a\\n" + b"test")
        result = ProviderOutputValidator(max_size_bytes=64).validate_file(path, modality="image")
        self.assertTrue(result["valid"])
        self.assertFalse(result["image_dimensions_validated"])
        self.assertNotIn("width", result)

    def test_invalid_image_dimension_limits_are_rejected(self):
        for kwargs in (
            {"max_image_dimension": 0},
            {"max_image_dimension": True},
            {"max_image_pixels": 0},
            {"max_image_pixels": False},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ProviderOutputValidator(**kwargs)

if __name__ == "__main__":
    unittest.main()
