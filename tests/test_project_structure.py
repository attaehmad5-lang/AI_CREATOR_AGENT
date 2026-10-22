"""Regression tests for the Project Structure / Folders feature."""
import tempfile
import unittest
from pathlib import Path

from projects.documentary_project import DEFAULT_FOLDERS, create_project_structure


class ProjectStructureTests(unittest.TestCase):
    def test_creates_all_default_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            result = create_project_structure(root)
            self.assertEqual([p.name for p in result], list(DEFAULT_FOLDERS))
            self.assertTrue(all(p.is_dir() for p in result))

    def test_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            first = create_project_structure(root)
            second = create_project_structure(root)
            self.assertEqual(first, second)
            self.assertTrue(all(p.is_dir() for p in second))

    def test_supports_nested_relative_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            result = create_project_structure(root, ("story/outline", "story/scenes"))
            self.assertTrue(all(p.is_dir() for p in result))

    def test_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            with self.assertRaises(ValueError):
                create_project_structure(root, ("../outside",))

    def test_rejects_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            with self.assertRaises(ValueError):
                create_project_structure(root, (str(Path(tmp).resolve()),))

    def test_rejects_empty_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            with self.assertRaises(ValueError):
                create_project_structure(root, ("",))


if __name__ == "__main__":
    unittest.main()
