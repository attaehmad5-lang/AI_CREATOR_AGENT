"""Audio/video container structure validation tests."""
import tempfile
from unittest.mock import patch
import unittest
import wave
from pathlib import Path

from projects.provider_store.media_integrity_validator import MediaIntegrityValidator


class MediaIntegrityValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.validator = MediaIntegrityValidator(max_bytes=2048, max_duration_seconds=30)

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_wav(self, path):
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 800)

    def test_valid_wav_reports_duration_and_audio_metadata(self):
        path = self.root / "sample.wav"
        self.make_wav(path)
        result = self.validator.validate(path, modality="audio")
        self.assertTrue(result["valid"])
        self.assertEqual(result["container"], "WAV")
        self.assertAlmostEqual(result["duration_seconds"], 0.1)
        self.assertEqual(result["sample_rate"], 8000)

    def test_truncated_wav_is_rejected(self):
        path = self.root / "truncated.wav"
        self.make_wav(path)
        path.write_bytes(path.read_bytes()[:-20])
        result = self.validator.validate(path, modality="audio")
        self.assertFalse(result["valid"])

    def test_mp4_requires_bounded_valid_boxes(self):
        path = self.root / "sample.mp4"
        boxes = (
            (b"ftyp", b"isom0000"),
            (b"moov", (28).to_bytes(4, "big") + b"mvhd" + b"\x00" * 4 + b"\x00" * 8 + (1000).to_bytes(4, "big") + (1000).to_bytes(4, "big")),
            (b"mdat", b"frame"),
        )
        data = b"".join((8 + len(body)).to_bytes(4, "big") + kind + body for kind, body in boxes)
        path.write_bytes(data)
        result = self.validator.validate(path, modality="video")
        self.assertTrue(result["valid"])
        self.assertEqual(result["container"], "MP4")
        malformed = self.root / "bad.mp4"
        malformed.write_bytes((1000).to_bytes(4, "big") + b"ftyp" + b"tiny")
        self.assertEqual(self.validator.validate(malformed, modality="video")["status"], "MP4_BOX_SIZE_INVALID")

    def test_mp4_extracts_movie_duration_from_version_zero_mvhd(self):
        path = self.root / "duration.mp4"
        mvhd_payload = b"\x00\x00\x00\x00" + (0).to_bytes(4, "big") * 2 + (1000).to_bytes(4, "big") + (12500).to_bytes(4, "big")
        mvhd = (8 + len(mvhd_payload)).to_bytes(4, "big") + b"mvhd" + mvhd_payload
        moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
        ftyp_body = b"isom0000"
        ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
        mdat = (9).to_bytes(4, "big") + b"mdatx"
        path.write_bytes(ftyp + moov + mdat)

        result = self.validator.validate(path, modality="video")

        self.assertTrue(result["valid"])
        self.assertEqual(result["container"], "MP4")
        self.assertEqual(result["timescale"], 1000)
        self.assertEqual(result["duration_seconds"], 12.5)

    def test_mp4_extracts_version_one_extended_duration(self):
        path = self.root / "duration-v1.mp4"
        mvhd_payload = (
            b"\\x01\\x00\\x00\\x00"
            + (0).to_bytes(8, "big")
            + (0).to_bytes(8, "big")
            + (1000).to_bytes(4, "big")
            + (28765).to_bytes(8, "big")
        )
        mvhd = (8 + len(mvhd_payload)).to_bytes(4, "big") + b"mvhd" + mvhd_payload
        moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
        ftyp_body = b"isom0000"
        ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
        mdat = (9).to_bytes(4, "big") + b"mdatx"
        path.write_bytes(ftyp + moov + mdat)

        result = self.validator.validate(path, modality="video")

        self.assertTrue(result["valid"])
        self.assertEqual(result["timescale"], 1000)
        self.assertEqual(result["duration_seconds"], 28.765)
        self.assertEqual(result["container"], "MP4")

    def test_mp4_duration_limit_and_invalid_movie_header_fail_closed(self):
        path = self.root / "too-long.mp4"
        mvhd_payload = b"\x00\x00\x00\x00" + (0).to_bytes(4, "big") * 2 + (1).to_bytes(4, "big") + (31).to_bytes(4, "big")
        mvhd = (8 + len(mvhd_payload)).to_bytes(4, "big") + b"mvhd" + mvhd_payload
        moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
        ftyp_body = b"isom0000"
        ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
        mdat = (9).to_bytes(4, "big") + b"mdatx"
        path.write_bytes(ftyp + moov + mdat)
        self.assertEqual(
            self.validator.validate(path, modality="video")["status"],
            "MEDIA_DURATION_EXCEEDED",
        )

        malformed_mvhd = (10).to_bytes(4, "big") + b"mvhdxx"
        malformed_moov = (8 + len(malformed_mvhd)).to_bytes(4, "big") + b"moov" + malformed_mvhd
        path.write_bytes(ftyp + malformed_moov + mdat)
        self.assertEqual(
            self.validator.validate(path, modality="video")["status"],
            "MP4_MOVIE_HEADER_INVALID",
        )

    def test_mp4_extracts_version_one_duration_and_rejects_duplicate_movie_headers(self):
        path = self.root / "version-one.mp4"
        payload = (
            b"\x01\x00\x00\x00"
            + (0).to_bytes(8, "big") * 2
            + (1000).to_bytes(4, "big")
            + (25000).to_bytes(8, "big")
        )
        mvhd = (8 + len(payload)).to_bytes(4, "big") + b"mvhd" + payload
        moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
        ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
        mdat = (9).to_bytes(4, "big") + b"mdatx"
        path.write_bytes(ftyp + moov + mdat)
        result = self.validator.validate(path, modality="video")
        self.assertTrue(result["valid"])
        self.assertEqual(result["duration_seconds"], 25.0)

        duplicate = (8 + len(mvhd) * 2).to_bytes(4, "big") + b"moov" + mvhd + mvhd
        path.write_bytes(ftyp + duplicate + mdat)
        self.assertEqual(
            self.validator.validate(path, modality="video")["status"],
            "MP4_DUPLICATE_MOVIE_HEADER",
        )

    def test_mp4_movie_box_without_movie_header_is_rejected(self):
        path = self.root / "missing-mvhd.mp4"
        free = (28).to_bytes(4, "big") + b"mvhd" + b"\x00" * 4 + b"\x00" * 8 + (1000).to_bytes(4, "big") + (1000).to_bytes(4, "big")
        moov = (8 + len(free)).to_bytes(4, "big") + b"moov" + free
        ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
        mdat = (9).to_bytes(4, "big") + b"mdatx"
        path.write_bytes(ftyp + moov + mdat)
        self.assertEqual(
            self.validator.validate(path, modality="video")["status"],
            "MP4_MOVIE_HEADER_MISSING",
        )

    def test_mp4_validation_skips_media_payload_without_loading_whole_file(self):
        path = self.root / "large.mp4"
        boxes = (
            (b"ftyp", b"isom0000"),
            (b"moov", (28).to_bytes(4, "big") + b"mvhd" + b"\x00" * 4 + b"\x00" * 8 + (1000).to_bytes(4, "big") + (1000).to_bytes(4, "big")),
            (b"mdat", b"x" * 1_000_000),
        )
        path.write_bytes(
            b"".join(
                (8 + len(body)).to_bytes(4, "big") + kind + body
                for kind, body in boxes
            )
        )
        validator = MediaIntegrityValidator(max_bytes=2_000_000)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            result = validator.validate(path, modality="video")
        self.assertTrue(result["valid"])
        self.assertEqual(result["container"], "MP4")

    def test_ogg_pages_are_checked_incrementally(self):
        path = self.root / "sample.ogg"
        # One empty Ogg page: 27-byte fixed header, zero lacing entries and body.
        page = bytearray(27)
        page[:4] = b"OggS"
        page[4] = 0
        page[26] = 0
        path.write_bytes(bytes(page))
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")):
            result = self.validator.validate(path, modality="audio")
        self.assertTrue(result["valid"])
        self.assertEqual(result["container"], "OGG")
        self.assertEqual(result["page_count"], 1)

    def test_webm_requires_ebml_header_and_segment(self):
        path = self.root / "sample.webm"
        path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x42\x86\x81\x01" + b"\x18\x53\x80\x67" + b"\x00")
        self.assertTrue(self.validator.validate(path, modality="video")["valid"])
        bad = self.root / "bad.webm"
        bad.write_bytes(b"\x1a\x45\xdf\xa3")
        self.assertEqual(self.validator.validate(bad, modality="video")["status"], "WEBM_SEGMENT_MISSING")

    def test_symlink_and_malformed_paths_are_rejected(self):
        source = self.root / "source.wav"
        self.make_wav(source)
        link = self.root / "linked.wav"
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        self.assertEqual(
            self.validator.validate(link, modality="audio")["status"],
            "SYMLINK_REJECTED",
        )
        self.assertEqual(
            self.validator.validate("bad" + chr(0) + "path.wav", modality="audio")["status"],
            "INVALID_MEDIA_PATH",
        )
        self.assertEqual(
            self.validator.validate(source, modality=["audio"])["status"],
            "UNSUPPORTED_MEDIA_MODALITY",
        )

    def test_unsupported_modality_and_size_limit_fail_closed(self):
        path = self.root / "unknown.bin"
        path.write_bytes(b"x" * 3000)
        self.assertEqual(self.validator.validate(path, modality="video")["status"], "MEDIA_FILE_TOO_LARGE")
        self.assertEqual(self.validator.validate(path, modality="text")["status"], "UNSUPPORTED_MEDIA_MODALITY")


if __name__ == "__main__":
    unittest.main()
