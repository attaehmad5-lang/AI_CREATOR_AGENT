"""Regression tests for safe workspace file operations."""
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from projects.folder_engine import FolderEngine, UnsafePathError
from projects.workspace_file_ops import WorkspaceFileOps


class WorkspaceFileOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.engine = FolderEngine(self.root)
        self.engine.ensure_root()
        self.files = WorkspaceFileOps(self.engine, max_file_bytes=64)

    def test_upload_download_and_checksum(self):
        payload = b"hello world"
        info = self.files.upload_bytes("research/note.txt", payload)
        self.assertEqual(self.files.download_bytes("research/note.txt"), payload)
        self.assertEqual(info["sha256"], self.files.sha256_bytes(payload))
        self.assertEqual(info["size"], len(payload))

    def test_upload_rejects_oversize_and_existing_destination(self):
        with self.assertRaises(ValueError):
            self.files.upload_bytes("large.bin", b"x" * 65)
        self.files.upload_bytes("note.txt", b"first")
        with self.assertRaises(FileExistsError):
            self.files.upload_bytes("note.txt", b"second")
        self.assertEqual(self.files.download_bytes("note.txt"), b"first")

    def test_upload_overwrite_is_explicit(self):
        self.files.upload_bytes("note.txt", b"first")
        self.files.upload_bytes("note.txt", b"second", overwrite=True)
        self.assertEqual(self.files.download_bytes("note.txt"), b"second")

    def test_rejects_absolute_and_traversal_paths(self):
        with self.assertRaises(UnsafePathError):
            self.files.upload_bytes("../outside.txt", b"x")
        with self.assertRaises(UnsafePathError):
            self.files.download_bytes(str(self.root / "outside.txt"))

    def test_describe_detects_png_magic_and_extension(self):
        info = self.files.upload_bytes("image.png", b"\x89PNG\r\n\x1a\nrest")
        self.assertEqual(info["mime_type"], "image/png")
        self.assertEqual(info["extension"], ".png")

    def test_rename_move_copy_and_delete(self):
        self.files.upload_bytes("source.txt", b"content")
        self.files.rename("source.txt", "renamed.txt")
        self.files.copy("renamed.txt", "copies/copy.txt")
        self.files.move("copies/copy.txt", "archive/moved.txt")
        self.assertEqual(self.files.download_bytes("archive/moved.txt"), b"content")
        self.files.delete("renamed.txt")
        self.assertFalse((self.root / "renamed.txt").exists())

    def test_file_collision_is_not_overwritten_by_copy(self):
        self.files.upload_bytes("a.txt", b"a")
        self.files.upload_bytes("b.txt", b"b")
        with self.assertRaises(FileExistsError):
            self.files.copy("a.txt", "b.txt")
        self.assertEqual(self.files.download_bytes("b.txt"), b"b")

    def test_bulk_rename_supports_swaps(self):
        self.files.upload_bytes("a.txt", b"A")
        self.files.upload_bytes("b.txt", b"B")
        self.files.bulk_rename([("a.txt", "b.txt"), ("b.txt", "a.txt")])
        self.assertEqual(self.files.download_bytes("a.txt"), b"B")
        self.assertEqual(self.files.download_bytes("b.txt"), b"A")

    def test_bulk_rename_preflights_duplicate_destinations(self):
        self.files.upload_bytes("a.txt", b"A")
        self.files.upload_bytes("b.txt", b"B")
        with self.assertRaises(FileExistsError):
            self.files.bulk_rename([("a.txt", "same.txt"), ("b.txt", "same.txt")])
        self.assertTrue((self.root / "a.txt").exists())
        self.assertTrue((self.root / "b.txt").exists())

    def test_copy_preserves_existing_destination_on_failure(self):
        self.files.upload_bytes("source.bin", b"source")
        self.files.upload_bytes("destination.bin", b"keep")
        with self.assertRaises(FileExistsError):
            self.files.copy("source.bin", "destination.bin")
        self.assertEqual(self.files.download_bytes("destination.bin"), b"keep")

    def test_copy_creates_parent_and_publishes_complete_content(self):
        payload = b"complete payload" * 3
        self.files.upload_bytes("source.bin", payload)
        result = self.files.copy("source.bin", "nested/deeper/copy.bin")
        self.assertEqual(self.files.download_bytes("nested/deeper/copy.bin"), payload)
        self.assertEqual(result["sha256"], self.files.sha256_bytes(payload))
        self.assertFalse(list((self.root / "nested/deeper").glob(".copy-*.tmp")))

    def test_download_rejects_oversize_existing_file(self):
        (self.root / "big.bin").write_bytes(b"x" * 65)
        with self.assertRaises(ValueError):
            self.files.download_bytes("big.bin")


    def test_bulk_rename_restores_files_if_staging_fails(self):
        self.files.upload_bytes("a.txt", b"A")
        self.files.upload_bytes("b.txt", b"B")
        original_rename = Path.rename

        def fail_during_staging(path, target):
            if path.name == "b.txt" and Path(target).name.startswith(".rename-"):
                raise OSError("simulated staging failure")
            return original_rename(path, target)

        with mock.patch.object(Path, "rename", autospec=True, side_effect=fail_during_staging):
            with self.assertRaisesRegex(OSError, "simulated staging failure"):
                self.files.bulk_rename([("a.txt", "new-a.txt"), ("b.txt", "new-b.txt")])
        self.assertEqual(self.files.download_bytes("a.txt"), b"A")
        self.assertEqual(self.files.download_bytes("b.txt"), b"B")
        self.assertEqual(list(self.root.glob(".rename-*")), [])


if __name__ == "__main__":
    unittest.main()
