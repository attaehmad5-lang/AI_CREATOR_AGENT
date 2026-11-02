import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.generation.providers.atomic_output import (
    atomic_save_image,
    atomic_write_bytes,
)


class FakeImage:
    def __init__(self):
        self.formats = []

    def save(self, output, format):
        self.formats.append(format)
        output.write(b"\xff\xd8\xfffake-jpeg")


class AtomicProviderOutputTests(unittest.TestCase):
    def test_atomic_write_replaces_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "nested" / "output.bin"
            destination.parent.mkdir()
            destination.write_bytes(b"previous")
            result = atomic_write_bytes(destination, b"new-output")
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), b"new-output")
            self.assertEqual(list(destination.parent.glob("*.tmp")), [])

    def test_failed_replace_preserves_previous_output_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"previous-good-output")
            with patch(
                "projects.generation.providers.atomic_output.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_bytes(destination, b"candidate-output")
            self.assertEqual(destination.read_bytes(), b"previous-good-output")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_bounds_reject_empty_oversized_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "output.bin"
            destination.write_bytes(b"previous")
            for payload, limit in ((b"", 10), (b"too-large", 3)):
                with self.subTest(payload=payload):
                    with self.assertRaises(ValueError):
                        atomic_write_bytes(destination, payload, max_bytes=limit)
            with self.assertRaises(ValueError):
                atomic_write_bytes(destination, b"ok", max_bytes=True)
            self.assertEqual(destination.read_bytes(), b"previous")

    def test_image_is_encoded_for_destination_extension_before_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cover.jpg"
            image = FakeImage()
            result = atomic_save_image(image, destination)
            self.assertEqual(image.formats, ["JPEG"])
            self.assertEqual(result, destination)
            self.assertTrue(destination.read_bytes().startswith(b"\xff\xd8\xff"))

    def test_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.bin"
            target.write_bytes(b"target")
            link = root / "link.bin"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")
            with self.assertRaises(ValueError):
                atomic_write_bytes(link, b"replacement")
            self.assertEqual(target.read_bytes(), b"target")


if __name__ == "__main__":
    unittest.main()
