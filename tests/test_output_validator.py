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

    @staticmethod
    def png_header(width, height):
        return (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width & 0xFFFFFFFF, height & 0xFFFFFFFF)
            + bytes((8, 2, 0, 0, 0)) + b"\x00\x00\x00\x00"
        )

    @staticmethod
    def wav_bytes(data_bytes=80, sample_rate=8000, channels=1, bits_per_sample=8, format_code=1):
        block_align = channels * ((bits_per_sample + 7) // 8)
        byte_rate = sample_rate * block_align
        fmt = struct.pack(
            "<HHIIHH", format_code, channels, sample_rate, byte_rate,
            block_align, bits_per_sample,
        )
        data = bytes(data_bytes)
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        chunks += b"data" + struct.pack("<I", len(data)) + data
        if len(data) & 1:
            chunks += b"\x00"
        body = b"WAVE" + chunks
        return b"RIFF" + struct.pack("<I", len(body)) + body

    @staticmethod
    def avi_bytes(codec="H264", frames=4, micros_per_frame=500000, width=320, height=240):
        avih = bytearray(40)
        struct.pack_into("<I", avih, 0, micros_per_frame)
        struct.pack_into("<I", avih, 16, frames)
        struct.pack_into("<I", avih, 32, width)
        struct.pack_into("<I", avih, 36, height)
        avih_chunk = b"avih" + struct.pack("<I", len(avih)) + bytes(avih)
        strh_data = b"vids" + codec.encode("ascii") + bytes(48)
        strh_chunk = b"strh" + struct.pack("<I", len(strh_data)) + strh_data
        body = b"AVI " + avih_chunk + strh_chunk
        return b"RIFF" + struct.pack("<I", len(body)) + body

    @staticmethod
    def mp4_bytes(codec="avc1", timescale=1000, duration=2000):
        def box(kind, payload):
            return struct.pack(">I", 8 + len(payload)) + kind + payload
        mvhd = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, timescale, duration)
        stsd = b"\x00\x00\x00\x00" + struct.pack(">I", 1) + struct.pack(">I", 8) + codec.encode("ascii")
        return box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", box(b"mvhd", mvhd) + box(b"stsd", stsd))

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

    @staticmethod
    def flac_bytes(sample_rate=44100, channels=2, bits_per_sample=16, total_samples=44100):
        packed = (
            (sample_rate << 44)
            | ((channels - 1) << 41)
            | ((bits_per_sample - 1) << 36)
            | total_samples
        )
        streaminfo = bytes(10) + packed.to_bytes(8, "big") + bytes(16)
        return b"fLaC" + bytes((0x80, 0, 0, 34)) + streaminfo

    @staticmethod
    def webm_bytes(codec="V_VP9", duration=2.0, timecode_scale=1_000_000):
        def element(element_id, value):
            return element_id + bytes((0x80 | len(value),)) + value
        scale = element(bytes((0x2A, 0xD7, 0xB1)), timecode_scale.to_bytes(3, "big"))
        duration_element = element(bytes((0x44, 0x89)), struct.pack(">d", duration))
        return bytes((0x1A, 0x45, 0xDF, 0xA3)) + scale + duration_element + codec.encode("ascii")

    def test_flac_streaminfo_duration_and_sample_rate_are_validated(self):
        path = self.root / "song.flac"
        path.write_bytes(self.flac_bytes(sample_rate=44100, total_samples=88200))
        validator = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=3)
        result = validator.validate_file(path, modality="audio")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["codec"], "flac")
        self.assertEqual(result["duration_seconds"], 2.0)
        self.assertEqual(result["sample_rate"], 44100)
        long_limit = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=1)
        self.assertEqual(long_limit.validate_file(path, modality="audio")["status"], "MEDIA_DURATION_LIMIT_EXCEEDED")

    def test_webm_duration_and_codec_constraints(self):
        path = self.root / "clip.webm"
        path.write_bytes(self.webm_bytes(codec="V_VP9", duration=2.0))
        validator = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=3)
        result = validator.validate_file(path, modality="video")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["codec"], "vp9")
        self.assertEqual(result["duration_seconds"], 2.0)
        short_limit = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=1)
        self.assertEqual(short_limit.validate_file(path, modality="video")["status"], "MEDIA_DURATION_LIMIT_EXCEEDED")
        codec_limit = ProviderOutputValidator(max_size_bytes=1024, allowed_video_codecs={"avc1"})
        self.assertEqual(codec_limit.validate_file(path, modality="video")["status"], "MEDIA_CODEC_NOT_ALLOWED")

    def test_media_duration_limit_50_case_matrix(self):
        validator = ProviderOutputValidator(max_size_bytes=5000, max_media_duration_seconds=0.1)
        valid_sizes = [index * 8 for index in range(1, 26)]
        invalid_sizes = [800 + index * 8 for index in range(1, 26)]
        self.assertEqual(len(valid_sizes) + len(invalid_sizes), 50)
        for index, data_size in enumerate(valid_sizes):
            with self.subTest(case=index, data_size=data_size):
                path = self.root / f"valid-audio-{index}.wav"
                path.write_bytes(self.wav_bytes(data_bytes=data_size))
                result = validator.validate_file(path, modality="audio")
                self.assertTrue(result["valid"], result)
                self.assertLessEqual(result["duration_seconds"], 0.1)
                self.assertEqual(result["codec"], "pcm")
        for index, data_size in enumerate(invalid_sizes):
            with self.subTest(case=index + 25, data_size=data_size):
                path = self.root / f"long-audio-{index}.wav"
                path.write_bytes(self.wav_bytes(data_bytes=data_size))
                result = validator.validate_file(path, modality="audio")
                self.assertFalse(result["valid"], result)
                self.assertEqual(result["status"], "MEDIA_DURATION_LIMIT_EXCEEDED")

    def test_wav_codec_allowlist_and_malformed_riff_are_rejected(self):
        path = self.root / "codec.wav"
        path.write_bytes(self.wav_bytes())
        restricted = ProviderOutputValidator(max_size_bytes=1024, allowed_audio_codecs={"flac"})
        result = restricted.validate_file(path, modality="audio")
        self.assertEqual(result["status"], "MEDIA_CODEC_NOT_ALLOWED")
        malformed = bytearray(self.wav_bytes())
        malformed[4:8] = struct.pack("<I", len(malformed) + 100)
        path.write_bytes(malformed)
        self.assertEqual(
            ProviderOutputValidator(max_size_bytes=1024).validate_file(path, modality="audio")["status"],
            "MEDIA_PROPERTIES_UNVERIFIED",
        )

    def test_avi_duration_dimensions_and_codec_constraints(self):
        path = self.root / "clip.avi"
        path.write_bytes(self.avi_bytes(codec="H264", frames=4, micros_per_frame=500000, width=320, height=240))
        result = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=3).validate_file(path, modality="video")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["duration_seconds"], 2.0)
        self.assertEqual((result["width"], result["height"]), (320, 240))
        short_limit = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=1)
        self.assertEqual(short_limit.validate_file(path, modality="video")["status"], "MEDIA_DURATION_LIMIT_EXCEEDED")
        codec_limit = ProviderOutputValidator(max_size_bytes=1024, allowed_video_codecs={"av1"})
        self.assertEqual(codec_limit.validate_file(path, modality="video")["status"], "MEDIA_CODEC_NOT_ALLOWED")

    def test_mp4_duration_and_codec_constraints(self):
        path = self.root / "clip.mp4"
        path.write_bytes(self.mp4_bytes(codec="avc1", timescale=1000, duration=2000))
        validator = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=3)
        result = validator.validate_file(path, modality="video")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["codec"], "avc1")
        self.assertEqual(result["duration_seconds"], 2.0)
        short_limit = ProviderOutputValidator(max_size_bytes=1024, max_media_duration_seconds=1)
        self.assertEqual(short_limit.validate_file(path, modality="video")["status"], "MEDIA_DURATION_LIMIT_EXCEEDED")
        codec_limit = ProviderOutputValidator(max_size_bytes=1024, allowed_video_codecs={"av01"})
        self.assertEqual(codec_limit.validate_file(path, modality="video")["status"], "MEDIA_CODEC_NOT_ALLOWED")

    def test_media_limit_configuration_is_bounded(self):
        for value in (0, -1, True, float("inf"), 86401):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProviderOutputValidator(max_media_duration_seconds=value)

    def test_unparseable_image_dimensions_fail_closed(self):
        path = self.root / "truncated.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        result = self.validator.validate_file(path, modality="image")
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "IMAGE_DIMENSIONS_UNVERIFIED")

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
