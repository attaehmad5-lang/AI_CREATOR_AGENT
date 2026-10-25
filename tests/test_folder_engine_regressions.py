"""Regression tests for security and atomicity boundaries in FolderEngine.

Run with: python -m unittest tests.test_folder_engine_regressions
"""
import tempfile
import unittest
from pathlib import Path

from projects.folder_engine import (
    FolderEngine,
    FolderTypeError,
    UnsafePathError,
)


class FolderEngineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.engine = FolderEngine(self.root)

    def test_batch_validates_all_paths_before_creating_any(self):
        with self.assertRaises(UnsafePathError):
            self.engine.ensure_folders(["safe/first", "../escape"])
        self.assertFalse((self.root / "safe").exists())

    def test_batch_rejects_file_collision_before_creating_any(self):
        self.engine.ensure_root()
        (self.root / "blocked").write_text("not a directory", encoding="utf-8")
        with self.assertRaises(FolderTypeError):
            self.engine.ensure_folders(["new/first", "blocked/child"])
        self.assertFalse((self.root / "new").exists())

    def test_reserved_windows_device_names_are_rejected_portably(self):
        for name in ("CON", "nul.txt", "COM1", "LPT9.log"):
            with self.subTest(name=name):
                with self.assertRaises(UnsafePathError):
                    self.engine.ensure_folder(name)
        self.assertFalse(self.root.exists())

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.engine.ensure_root()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are not available on this platform")
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("linked/child")
        self.assertFalse((outside / "child").exists())

    def test_metadata_symlink_is_never_followed(self):
        self.engine.ensure_folder("alpha")
        outside = Path(self.temp.name) / "external-metadata.json"
        outside.write_text('{"version":1,"folders":{}}', encoding="utf-8")
        metadata = self.root / ".ai_creator_agent_folder_metadata.json"
        try:
            metadata.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks are not available on this platform")
        with self.assertRaises(UnsafePathError):
            self.engine.get_folder_id("alpha")
        self.assertEqual(outside.read_text(encoding="utf-8"), '{"version":1,"folders":{}}')

    def test_copy_folder_creates_independent_folder_ids(self):
        self.engine.ensure_folder("original/child")
        original_id = self.engine.get_folder_id("original")
        child_id = self.engine.get_folder_id("original/child")
        self.engine.set_metadata("original/child", "purpose", "research")
        self.engine.copy_folder("original", "copy")
        self.assertNotEqual(self.engine.get_folder_id("copy"), original_id)
        self.assertNotEqual(self.engine.get_folder_id("copy/child"), child_id)
        self.assertEqual(self.engine.get_metadata("copy/child")["purpose"], "research")

    def test_move_into_descendant_is_rejected_without_mutation(self):
        self.engine.ensure_folder("parent/child")
        with self.assertRaises(UnsafePathError):
            self.engine.move_folder("parent", "parent/child/moved")
        self.assertTrue((self.root / "parent/child").is_dir())
        self.assertFalse((self.root / "parent/child/moved").exists())

    def test_tree_depth_rejects_bool_and_negative_values(self):
        self.engine.ensure_folder("a/b")
        for invalid in (True, -1, 1.5):
            with self.subTest(value=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    self.engine.list_tree(max_depth=invalid)

    def test_delete_nonempty_folder_requires_explicit_recursive_flag(self):
        self.engine.ensure_folder("parent/child")
        with self.assertRaises(OSError):
            self.engine.delete_folder("parent")
        self.assertTrue((self.root / "parent/child").is_dir())
        self.engine.delete_folder("parent", recursive=True)
        self.assertFalse((self.root / "parent").exists())


if __name__ == "__main__":
    unittest.main()
