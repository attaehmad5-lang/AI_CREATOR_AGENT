"""Tests for the core folder engine."""
import tempfile
import unittest
from pathlib import Path

from projects.folder_engine import FolderEngine, FolderTypeError, UnsafePathError


class FolderEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.engine = FolderEngine(self.root)

    def test_root_is_created_lazily(self):
        self.assertFalse(self.root.exists())
        self.engine.ensure_folder("research")
        self.assertTrue(self.root.is_dir())

    def test_nested_folder_creation(self):
        entry = self.engine.ensure_folder("story/scenes/scene-01")
        self.assertTrue(entry.is_directory)
        self.assertEqual(entry.relative_path, "story/scenes/scene-01")

    def test_idempotent_creation(self):
        first = self.engine.ensure_folder("assets")
        second = self.engine.ensure_folder("assets")
        self.assertEqual(first.absolute_path, second.absolute_path)

    def test_bulk_creation_preserves_order(self):
        entries = self.engine.ensure_folders(["video", "audio", "visuals"])
        self.assertEqual([e.name for e in entries], ["video", "audio", "visuals"])

    def test_rejects_empty_root(self):
        with self.assertRaises(ValueError):
            FolderEngine("")

    def test_rejects_empty_relative_path(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("")

    def test_rejects_absolute_path(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve(str(self.root / "escape"))

    def test_rejects_parent_traversal(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("../outside")

    def test_rejects_nested_parent_traversal(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("safe/../../outside")

    def test_rejects_windows_separator_on_all_platforms(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("safe\\\\..\\\\outside")

    def test_rejects_windows_drive_path_on_all_platforms(self):
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("C:\\\\outside")

    def test_rejects_symlink_escape(self):
        self.engine.ensure_root()
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(UnsafePathError):
            self.engine.resolve("escape/secret")

    def test_rejects_workspace_root_as_child_folder(self):
        with self.assertRaises(UnsafePathError):
            self.engine.ensure_folder(".")

    def test_rejects_path_separator_in_single_name(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_name("nested/folder")

    def test_rejects_single_backslash_in_name(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_name("folder\\child")

    def test_rejects_null_byte_in_name(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_name("folder\0child")

    def test_rejects_dot_names(self):
        for name in ("", ".", ".."):
            with self.subTest(name=name), self.assertRaises(UnsafePathError):
                self.engine.validate_name(name)

    def test_rejects_nonportable_windows_folder_names(self):
        for name in ("folder?", "folder*", "folder:", "folder.", "CON", "nul.txt", "COM1", "LPT9.log"):
            with self.subTest(name=name), self.assertRaises(UnsafePathError):
                self.engine.validate_name(name)

    def test_resolve_rejects_nonportable_nested_folder_names(self):
        for path in ("story/scene?", "story/CON", "story/trailing."):
            with self.subTest(path=path), self.assertRaises(UnsafePathError):
                self.engine.resolve(path)

    def test_rejects_trailing_whitespace_name(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_name("folder ")

    def test_describe_missing_folder_without_creating(self):
        entry = self.engine.describe("missing")
        self.assertFalse(entry.exists)
        self.assertFalse(self.root.exists())

    def test_describe_existing_folder(self):
        self.engine.ensure_folder("research")
        entry = self.engine.describe("research")
        self.assertTrue(entry.exists)
        self.assertTrue(entry.is_directory)

    def test_list_folders_is_sorted_and_excludes_files(self):
        self.engine.ensure_folders(["zeta", "Alpha", "beta"])
        (self.root / "note.txt").write_text("not a folder", encoding="utf-8")
        self.assertEqual([e.name for e in self.engine.list_folders()], ["Alpha", "beta", "zeta"])

    def test_list_folders_does_not_follow_directory_symlinks(self):
        self.engine.ensure_folder("inside")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.root / "linked-outside"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable on this platform")
        self.assertEqual(
            [entry.name for entry in self.engine.list_folders()],
            ["inside"],
        )

    def test_list_missing_path_is_empty(self):
        self.assertEqual(self.engine.list_folders("not-created"), [])

    def test_validate_structure_reports_valid_layout(self):
        self.engine.ensure_folders(["research", "story/scenes"])
        report = self.engine.validate_structure(["research", "story/scenes"])
        self.assertTrue(report.valid)
        self.assertEqual(report.checked_paths, ("research", "story/scenes"))
        self.assertEqual(report.missing_paths, ())
        self.assertEqual(report.non_directory_paths, ())

    def test_validate_structure_reports_missing_paths_without_creating_them(self):
        report = self.engine.validate_structure(["research", "story/scenes"])
        self.assertFalse(report.valid)
        self.assertEqual(report.missing_paths, ("research", "story/scenes"))
        self.assertFalse(self.root.exists())

    def test_validate_structure_reports_file_collisions_separately(self):
        self.engine.ensure_root()
        (self.root / "not-a-folder").write_text("x", encoding="utf-8")
        report = self.engine.validate_structure(["missing", "not-a-folder"])
        self.assertFalse(report.valid)
        self.assertEqual(report.missing_paths, ("missing",))
        self.assertEqual(report.non_directory_paths, ("not-a-folder",))

    def test_validate_structure_rejects_unsafe_paths(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_structure(["../outside"])

    def test_validate_structure_rejects_root_as_child(self):
        with self.assertRaises(UnsafePathError):
            self.engine.validate_structure(["."])

    def test_validate_structure_rejects_single_string_argument(self):
        with self.assertRaises(TypeError):
            self.engine.validate_structure("research")

    def test_list_tree_preserves_nested_parent_child_relationships(self):
        self.engine.ensure_folders([
            "story/scenes/scene-02",
            "story/scenes/scene-01",
            "research",
        ])
        tree = self.engine.list_tree()
        self.assertEqual(len(tree), 1)
        root = tree[0]
        self.assertEqual(root.entry.relative_path, ".")
        self.assertEqual(
            [node.entry.name for node in root.children], ["research", "story"]
        )
        story = root.children[1]
        self.assertEqual([node.entry.name for node in story.children], ["scenes"])
        scenes = story.children[0]
        self.assertEqual(
            [node.entry.name for node in scenes.children], ["scene-01", "scene-02"]
        )

    def test_list_tree_depth_zero_returns_only_root(self):
        self.engine.ensure_folder("story/scenes")
        tree = self.engine.list_tree(max_depth=0)
        self.assertEqual(len(tree), 1)
        self.assertEqual(tree[0].entry.relative_path, ".")
        self.assertEqual(tree[0].children, ())

    def test_list_tree_depth_one_returns_immediate_children_only(self):
        self.engine.ensure_folder("story/scenes")
        tree = self.engine.list_tree(max_depth=1)
        story = next(node for node in tree[0].children if node.entry.name == "story")
        self.assertEqual(story.children, ())

    def test_list_tree_rejects_negative_depth(self):
        with self.assertRaises(ValueError):
            self.engine.list_tree(max_depth=-1)

    def test_list_tree_rejects_non_integer_depth(self):
        with self.assertRaises(TypeError):
            self.engine.list_tree(max_depth=1.5)

    def test_list_tree_missing_root_returns_empty_tuple(self):
        self.assertEqual(self.engine.list_tree("missing"), ())

    def test_list_tree_does_not_follow_symlinks(self):
        self.engine.ensure_root()
        outside = Path(self.tmp.name) / "outside-tree"
        outside.mkdir()
        link = self.root / "external-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        self.assertEqual(self.engine.list_tree()[0].children, ())

    def test_workspace_root_file_collision_is_reported(self):
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("occupied", encoding="utf-8")
        with self.assertRaises(FolderTypeError):
            self.engine.ensure_root()

    def test_file_collision_is_reported(self):
        self.engine.ensure_root()
        (self.root / "occupied").write_text("x", encoding="utf-8")
        with self.assertRaises(FolderTypeError):
            self.engine.ensure_folder("occupied")

    def test_single_path_is_not_accepted_as_bulk_iterable(self):
        with self.assertRaises(TypeError):
            self.engine.ensure_folders("research")

    def test_bulk_creation_prevalidates_all_paths_before_creating(self):
        with self.assertRaises(UnsafePathError):
            self.engine.ensure_folders(["safe-folder", "../outside"])
        self.assertFalse(self.root.exists())

    def test_bulk_creation_preflights_file_collisions_before_creating(self):
        self.engine.ensure_root()
        (self.root / "blocked").write_text("x", encoding="utf-8")
        with self.assertRaises(FolderTypeError):
            self.engine.ensure_folders(["safe-folder", "blocked/child"])
        self.assertFalse((self.root / "safe-folder").exists())

    def test_name_must_be_string(self):
        with self.assertRaises(TypeError):
            self.engine.validate_name(None)


    def test_folder_metadata_rejects_corrupt_json(self):
        self.engine.ensure_folder("research")
        self.engine._metadata_path().write_text("{broken", encoding="utf-8")
        with self.assertRaises(Exception):
            self.engine.get_folder_id("research")

    def test_metadata_key_validation_does_not_mutate_state(self):
        self.engine.ensure_folder("research")
        before = self.engine._metadata_path().read_bytes() if self.engine._metadata_path().exists() else None
        for key in ("", " title", "title "):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.engine.set_metadata("research", key, "value")
        after = self.engine._metadata_path().read_bytes() if self.engine._metadata_path().exists() else None
        self.assertEqual(before, after)

    def test_folder_id_read_only_does_not_create_metadata(self):
        self.engine.ensure_folder("research")
        self.assertIsNone(self.engine.get_folder_id("research", create=False))
        self.assertFalse(self.engine._metadata_path().exists())

    def test_metadata_nested_values_are_defensively_copied(self):
        self.engine.ensure_folder("research")
        self.engine.set_metadata("research", "tags", ["story", {"language": "ur"}])
        first = self.engine.get_metadata("research")
        first["tags"][1]["language"] = "changed"
        self.assertEqual(self.engine.get_metadata("research")["tags"][1]["language"], "ur")

    def test_no_import_time_side_effects_contract(self):
        # Constructing the engine does not create the root.
        other_root = self.root / "untouched"
        FolderEngine(other_root)
        self.assertFalse(other_root.exists())


    def test_folder_ids_are_stable_and_unique(self):
        self.engine.ensure_folders(["alpha", "beta"])
        alpha_id = self.engine.get_folder_id("alpha")
        self.assertEqual(self.engine.get_folder_id("alpha"), alpha_id)
        self.assertNotEqual(alpha_id, self.engine.get_folder_id("beta"))
        self.engine.ensure_folder("gamma")
        self.assertIsNone(self.engine.get_folder_id("gamma", create=False))

    def test_metadata_round_trip_and_defensive_copy(self):
        self.engine.ensure_folder("research")
        self.engine.set_metadata("research", "purpose", "fact-checking")
        self.engine.set_metadata("research", "tags", ["source", "verified"])
        data = self.engine.get_metadata("research")
        self.assertEqual(data["purpose"], "fact-checking")
        data["purpose"] = "mutated"
        self.assertEqual(self.engine.get_metadata("research")["purpose"], "fact-checking")
        self.assertTrue((self.root / ".ai_creator_agent_folder_metadata.json").is_file())

    def test_metadata_rejects_non_json_values(self):
        self.engine.ensure_folder("research")
        with self.assertRaises(TypeError):
            self.engine.set_metadata("research", "bad", object())

    def test_rename_folder_preserves_metadata_and_rejects_collision(self):
        self.engine.ensure_folder("before")
        self.engine.set_metadata("before", "owner", "creator")
        old_id = self.engine.get_folder_id("before")
        self.engine.ensure_folder("taken")
        with self.assertRaises(FileExistsError):
            self.engine.rename_folder("before", "taken")
        renamed = self.engine.rename_folder("before", "after")
        self.assertEqual(renamed.relative_path, "after")
        self.assertEqual(self.engine.get_folder_id("after"), old_id)
        self.assertEqual(self.engine.get_metadata("after")["owner"], "creator")

    def test_move_folder_updates_metadata_paths(self):
        self.engine.ensure_folder("source/nested")
        self.engine.set_metadata("source/nested", "kind", "scene")
        nested_id = self.engine.get_folder_id("source/nested")
        moved = self.engine.move_folder("source", "archive/source")
        self.assertEqual(moved.relative_path, "archive/source")
        self.assertEqual(self.engine.get_folder_id("archive/source/nested"), nested_id)
        self.assertEqual(self.engine.get_metadata("archive/source/nested")["kind"], "scene")

    def test_move_rejects_descendant_and_existing_destination(self):
        self.engine.ensure_folder("source/child")
        with self.assertRaises(UnsafePathError):
            self.engine.move_folder("source", "source/child/moved")
        self.engine.ensure_folder("taken")
        with self.assertRaises(FileExistsError):
            self.engine.move_folder("source", "taken")

    def test_copy_folder_copies_tree_without_overwriting(self):
        self.engine.ensure_folder("source/nested")
        (self.root / "source" / "note.txt").write_text("hello", encoding="utf-8")
        copied = self.engine.copy_folder("source", "copies/source")
        self.assertEqual(copied.relative_path, "copies/source")
        self.assertEqual((self.root / "copies/source/note.txt").read_text(encoding="utf-8"), "hello")
        with self.assertRaises(FileExistsError):
            self.engine.copy_folder("source", "copies/source")

    def test_copy_rejects_copy_into_descendant(self):
        self.engine.ensure_folder("source/child")
        with self.assertRaises(UnsafePathError):
            self.engine.copy_folder("source", "source/child/copy")

    def test_delete_requires_explicit_recursive_flag_and_cleans_metadata(self):
        self.engine.ensure_folder("source/nested")
        self.engine.set_metadata("source/nested", "kind", "scene")
        with self.assertRaises(OSError):
            self.engine.delete_folder("source")
        self.engine.delete_folder("source", recursive=True)
        self.assertFalse((self.root / "source").exists())
        data = self.engine._read_metadata()
        self.assertEqual(data["folders"], {})


    def test_rename_folder_preserves_descendant_metadata_paths(self):
        self.engine.ensure_folder("parent/child")
        child_id = self.engine.get_folder_id("parent/child")
        self.engine.set_metadata("parent/child", "purpose", "scene")
        self.engine.rename_folder("parent", "renamed")
        self.assertEqual(self.engine.get_folder_id("renamed/child"), child_id)
        self.assertEqual(self.engine.get_metadata("renamed/child")["purpose"], "scene")

    def test_copy_folder_copies_metadata_but_regenerates_ids(self):
        self.engine.ensure_folder("source/nested")
        self.engine.set_metadata("source/nested", "purpose", "research")
        source_id = self.engine.get_folder_id("source/nested")
        self.engine.copy_folder("source", "copy")
        self.assertEqual(self.engine.get_metadata("copy/nested")["purpose"], "research")
        self.assertNotEqual(self.engine.get_folder_id("copy/nested"), source_id)


if __name__ == "__main__":
    unittest.main()
