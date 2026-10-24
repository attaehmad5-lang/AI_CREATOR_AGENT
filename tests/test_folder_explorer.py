"""Tests for persistent folder explorer navigation and preferences."""
import tempfile
import unittest
from pathlib import Path

from projects.folder_engine import FolderEngine, FolderEngineError, UnsafePathError
from projects.folder_explorer import FolderExplorer


class FolderExplorerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.engine = FolderEngine(self.root)
        self.engine.ensure_folders(["stories/scene-01", "stories/scene-02", "research", "assets/audio"])
        self.explorer = FolderExplorer(self.engine)

    def test_navigation_breadcrumbs_back_and_forward(self):
        self.assertEqual(self.explorer.navigate("stories"), "stories")
        self.explorer.navigate("stories/scene-01")
        self.assertEqual([x["path"] for x in self.explorer.breadcrumbs()],
                         [".", "stories", "stories/scene-01"])
        self.assertEqual(self.explorer.back(), "stories")
        self.assertEqual(self.explorer.forward(), "stories/scene-01")

    def test_navigation_rejects_traversal(self):
        with self.assertRaises(UnsafePathError):
            self.explorer.navigate("../outside")

    def test_recent_favorites_and_pins_are_persistent(self):
        self.explorer.navigate("research")
        self.explorer.set_favorite("research")
        self.explorer.set_pinned("stories")
        second = FolderExplorer(self.engine)
        self.assertEqual(second.recent(), ["research"])
        self.assertEqual(second.favorites(), ["research"])
        self.assertEqual(second.pinned(), ["stories"])

    def test_root_cannot_be_favorited_or_pinned(self):
        with self.assertRaises(UnsafePathError):
            self.explorer.set_favorite(".")
        with self.assertRaises(UnsafePathError):
            self.explorer.set_pinned(".")

    def test_view_preference_validation_and_persistence(self):
        self.explorer.set_view("grid")
        self.assertEqual(FolderExplorer(self.engine).view, "grid")
        with self.assertRaises(ValueError):
            self.explorer.set_view("timeline")

    def test_multifield_sorting_and_name_filter(self):
        self.explorer.set_sort([("name", True)])
        self.explorer.set_filter(name_contains="scene-02")
        tree = self.explorer.tree("stories")
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0].entry.name, "stories")
        self.assertEqual([node.entry.name for node in tree[0].children], ["scene-02"])

    def test_path_prefix_filter(self):
        self.explorer.set_filter(path_prefix="assets")
        tree = self.explorer.tree(".")
        self.assertEqual([node.entry.name for node in tree], [self.root.name])
        self.assertEqual([node.entry.name for node in tree[0].children], ["assets"])

    def test_labels_icons_and_overview(self):
        self.explorer.set_label("research", "Fact checking")
        self.explorer.set_icon("research", "book-open")
        overview = self.explorer.overview("research")
        self.assertEqual(overview["label"], "Fact checking")
        self.assertEqual(overview["icon"], "book-open")
        self.assertTrue((self.root / ".folder_explorer_state.json").is_file())

    def test_label_and_icon_can_be_removed(self):
        self.explorer.set_label("research", "Temporary")
        self.explorer.set_icon("research", "folder")
        self.explorer.set_label("research", None)
        self.explorer.set_icon("research", None)
        overview = self.explorer.overview("research")
        self.assertIsNone(overview["label"])
        self.assertIsNone(overview["icon"])

    def test_history_is_bounded(self):
        explorer = FolderExplorer(self.engine, history_limit=2)
        explorer.navigate("research")
        explorer.navigate("stories")
        explorer.navigate("assets")
        self.assertEqual(explorer.back(), "stories")
        self.assertEqual(explorer.back(), "research")
        self.assertEqual(explorer.back(), "research")

    def test_invalid_history_limit(self):
        with self.assertRaises(ValueError):
            FolderExplorer(self.engine, history_limit=0)

    def test_rejects_symlinked_state_file(self):
        import json
        import shutil
        state = self.root / ".folder_explorer_state.json"
        state.unlink(missing_ok=True)
        outside = Path(self.tmp.name) / "outside-state.json"
        outside.write_text(json.dumps({"version": 1, "recent": []}), encoding="utf-8")
        try:
            state.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable on this platform")
        with self.assertRaises(UnsafePathError):
            FolderExplorer(self.engine)

    def test_rejects_unsafe_persisted_recent_path(self):
        import json
        state = self.root / ".folder_explorer_state.json"
        state.write_text(json.dumps({"version": 1, "recent": ["../outside"]}), encoding="utf-8")
        with self.assertRaises(FolderEngineError):
            FolderExplorer(self.engine)


    def test_rejects_non_integer_or_boolean_persisted_schema_version(self):
        import json
        state = self.root / ".folder_explorer_state.json"
        for version in (True, False, "1", 1.0, 2):
            with self.subTest(version=version):
                state.write_text(json.dumps({"version": version}), encoding="utf-8")
                with self.assertRaises(FolderEngineError):
                    FolderExplorer(self.engine)


if __name__ == "__main__":
    unittest.main()
