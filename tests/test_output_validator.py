import json
import stat
import struct
import zlib
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
        self.validator = ProviderOutputValidator(max_size_bytes=256)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def png_chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    @classmethod
    def png_header(cls, width, height):
        ihdr = (
            struct.pack(">II", width & 0xFFFFFFFF, height & 0xFFFFFFFF)
            + bytes((8, 2, 0, 0, 0))
        )
        return (
            b"\x89PNG\r\n\x1a\n"
            + cls.png_chunk(b"IHDR", ihdr)
            + cls.png_chunk(b"IDAT", zlib.compress(b"\x00"))
            + cls.png_chunk(b"IEND", b"")
        )

    def test_valid_image_signature_returns_hash_and_metadata(self):
        path = self.root / "cover.png"
        path.write_bytes(self.png_header(320, 240))
        result = self.validator.validate_file(path, modality="image")
        self.assertTrue(result["valid"])
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual((result["width"], result["height"]), (320, 240))
        self.assertEqual(len(result["sha256"]), 64)

    def test_image_dimension_validation_50_case_matrix(self):
        validator = ProviderOutputValidator(
            max_size_bytes=128, max_image_dimension=100, max_image_pixels=1000
        )
        valid_dimensions = [(index, 1) for index in range(1, 26)]
        invalid_dimensions = [
            (0, 1), (1, 0), (-1, 1), (1, -1),
            (101, 1), (1, 101), (1000, 1), (1, 1000),
            (50, 21), (21, 50), (34, 30), (30, 34),
            (100, 11), (11, 100), (99, 11), (11, 99),
            (200, 2), (2, 200), (100, 100), (101, 10),
            (10, 101), (16385, 1), (1, 16385), (500, 500),
            (100000, 1),
        ]
        self.assertEqual(len(valid_dimensions) + len(invalid_dimensions), 50)
        for index, (width, height) in enumerate(valid_dimensions):
            with self.subTest(case=index, dimensions=(width, height)):
                path = self.root / f"valid-{index}.png"
                path.write_bytes(self.png_header(width, height))
                result = validator.validate_file(path, modality="image")
                self.assertTrue(result["valid"], result)
                self.assertEqual((result["width"], result["height"]), (width, height))
        for index, (width, height) in enumerate(invalid_dimensions):
            with self.subTest(case=index + 25, dimensions=(width, height)):
                path = self.root / f"invalid-{index}.png"
                path.write_bytes(self.png_header(width, height))
                result = validator.validate_file(path, modality="image")
                self.assertFalse(result["valid"], result)
                self.assertIn(result["status"], {
                    "IMAGE_DIMENSIONS_INVALID",
                    "IMAGE_DIMENSION_LIMIT_EXCEEDED",
                    "IMAGE_PIXEL_LIMIT_EXCEEDED",
                })

    def test_png_structure_rejects_50_corruptions(self):
        validator = ProviderOutputValidator(max_size_bytes=1024)
        valid = self.png_header(16, 16)
        ihdr_crc = 8 + 8 + 13
        idat_start = 8 + 25
        idat_length = int.from_bytes(valid[idat_start:idat_start + 4], "big")
        idat_crc = idat_start + 8 + idat_length
        iend_crc = idat_crc + 4 + 8
        crc_offsets = list(range(ihdr_crc, ihdr_crc + 4))
        crc_offsets += list(range(idat_crc, idat_crc + 4))
        crc_offsets += list(range(iend_crc, iend_crc + 4))
        truncations = [valid[:index] for index in range(25)]
        crc_corruptions = []
        for index in range(25):
            payload = bytearray(valid)
            offset = crc_offsets[index % len(crc_offsets)]
            payload[offset] ^= 1 << (index % 8)
            crc_corruptions.append(bytes(payload))
        cases = truncations + crc_corruptions
        self.assertEqual(len(cases), 50)
        for index, payload in enumerate(cases):
            with self.subTest(case=index, payload_size=len(payload)):
                path = self.root / f"corrupt-{index}.png"
                path.write_bytes(payload)
                result = validator.validate_file(path, modality="image")
                self.assertFalse(result["valid"], result)

    def test_unparseable_image_dimensions_fail_closed(self):
        path = self.root / "truncated.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        result = self.validator.validate_file(path, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "IMAGE_STRUCTURE_INVALID")

    def test_image_limit_configuration_is_bounded(self):
        invalid_options = [
            {"max_image_dimension": 0},
            {"max_image_dimension": -1},
            {"max_image_dimension": True},
            {"max_image_dimension": 1_000_001},
            {"max_image_pixels": 0},
            {"max_image_pixels": -1},
            {"max_image_pixels": False},
            {"max_image_pixels": 10**13},
        ]
        for index, options in enumerate(invalid_options):
            with self.subTest(case=index, options=options):
                with self.assertRaises(ValueError):
                    ProviderOutputValidator(**options)

    def test_gif_and_jpeg_dimensions_are_parsed(self):
        gif = b"GIF89a" + struct.pack("<HH", 12, 7) + bytes(20)
        self.assertEqual(
            ProviderOutputValidator._image_dimensions("image/gif", gif), (12, 7)
        )
        jpeg = (
            b"\xff\xd8\xff\xc0"
            + struct.pack(">H", 8) + bytes([8])
            + struct.pack(">HH", 7, 12) + bytes([3])
        )
        self.assertEqual(
            ProviderOutputValidator._image_dimensions("image/jpeg", jpeg), (12, 7)
        )

    def test_webp_extended_and_lossless_dimensions_are_parsed(self):
        vp8x = (
            b"RIFF" + bytes(4) + b"WEBPVP8X" + bytes(4) + bytes(4)
            + (19).to_bytes(3, "little") + (9).to_bytes(3, "little")
        )
        self.assertEqual(
            ProviderOutputValidator._image_dimensions("image/webp", vp8x), (20, 10)
        )
        vp8l = b"RIFF" + bytes(4) + b"WEBPVP8L" + bytes(4) + bytes([0x2F, 4, 0, 1, 0])
        self.assertEqual(
            ProviderOutputValidator._image_dimensions("image/webp", vp8l), (5, 5)
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
        large.write_bytes(b"x" * 257)
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
        valid_png = self.png_header(16, 16)
        path.write_bytes(valid_png + b"extra bytes")
        actual = path.stat()
        fake_stat = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_size=len(valid_png),
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
