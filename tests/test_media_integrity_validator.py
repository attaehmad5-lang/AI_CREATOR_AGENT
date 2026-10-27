"""Audio/video container structure validation tests."""
import tempfile
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
            (b"moov", b"metadata"),
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

    def test_webm_requires_ebml_header_and_segment(self):
        path = self.root / "sample.webm"
        path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x42\x86\x81\x01" + b"\x18\x53\x80\x67" + b"\x00")
        self.assertTrue(self.validator.validate(path, modality="video")["valid"])
        bad = self.root / "bad.webm"
        bad.write_bytes(b"\x1a\x45\xdf\xa3")
        self.assertEqual(self.validator.validate(bad, modality="video")["status"], "WEBM_SEGMENT_MISSING")

    def test_malformed_arguments_fail_closed_without_raising(self):
        for modality in (None, [], {}, 1):
            with self.subTest(modality=modality):
                result = self.validator.validate("unused.wav", modality=modality)
                self.assertEqual(result["status"], "UNSUPPORTED_MEDIA_MODALITY")
                self.assertFalse(result["valid"])

        for path in (None, 1, object(), ""):
            with self.subTest(path_type=type(path).__name__):
                result = self.validator.validate(path, modality="audio")
                self.assertEqual(result["status"], "INVALID_MEDIA_PATH")
                self.assertFalse(result["valid"])

    def test_unsupported_modality_and_size_limit_fail_closed(self):
        path = self.root / "unknown.bin"
        path.write_bytes(b"x" * 3000)
        self.assertEqual(self.validator.validate(path, modality="video")["status"], "MEDIA_FILE_TOO_LARGE")
        self.assertEqual(self.validator.validate(path, modality="text")["status"], "UNSUPPORTED_MEDIA_MODALITY")


if __name__ == "__main__":
    unittest.main()
