"""Tests for bounded workspace search and saved queries."""
import tempfile
import unittest
from pathlib import Path

from projects.folder_engine import FolderEngine, UnsafePathError
from projects.workspace_search import WorkspaceSearch


class WorkspaceSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.engine = FolderEngine(self.root)
        self.engine.ensure_folders(["research", "stories/scenes"])
        (self.root / "research" / "facts.txt").write_text("Evidence-based story", encoding="utf-8")
        (self.root / "stories" / "scenes" / "scene-01.md").write_text("A dramatic scene", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\x00\xff")
        self.searcher = WorkspaceSearch(self.engine, max_file_bytes=128, max_results=10)

    def test_search_file_names_is_deterministic(self):
        results = self.searcher.search("scene", mode="name")
        self.assertEqual([r["path"] for r in results], ["stories/scenes/scene-01.md"])

    def test_search_text_content(self):
        results = self.searcher.search("evidence", mode="content")
        self.assertEqual([r["path"] for r in results], ["research/facts.txt"])

    def test_search_content_extension_filter(self):
        results = self.searcher.search("dramatic", mode="content", extensions=["md"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "scene-01.md")

    def test_search_regex_and_case_sensitivity(self):
        self.assertEqual(len(self.searcher.search(r"scene-\d+", regex=True)), 1)
        self.assertEqual(self.searcher.search("EVIDENCE", mode="content", case_sensitive=True), [])

    def test_search_limit_and_invalid_query(self):
        self.assertEqual(len(self.searcher.search(".", limit=1)), 1)
        with self.assertRaises(ValueError):
            self.searcher.search("")
        with self.assertRaises(ValueError):
            self.searcher.search("x", limit=0)

    def test_search_rejects_invalid_regex(self):
        with self.assertRaises(Exception):
            self.searcher.search("[", regex=True)

    def test_folder_search(self):
        results = self.searcher.search_folders("scene")
        self.assertEqual([r["path"] for r in results], ["stories/scenes"])

    def test_missing_search_root_returns_empty(self):
        self.assertEqual(self.searcher.search("x", within="not-created"), [])

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            self.searcher.search("x", within="../outside")


    def test_metadata_size_filters_and_sorting(self):
        small = self.root / "research" / "a.txt"
        small.write_text("x", encoding="utf-8")
        results = self.searcher.search("txt", mode="name", min_size=2, sort_by="size")
        self.assertTrue(all(item["size"] >= 2 for item in results))
        self.assertEqual(results, sorted(results, key=lambda item: (item["size"], item["path"].casefold(), item["path"])))

    def test_metadata_filter_validation(self):
        for kwargs in ({"min_size": -1}, {"min_size": 10, "max_size": 2},
                       {"sort_by": "owner"}, {"modified_after": 10, "modified_before": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                self.searcher.search("x", **kwargs)

    def test_hidden_files_excluded_by_default_and_can_be_included(self):
        hidden = self.root / ".private.txt"
        hidden.write_text("secret", encoding="utf-8")
        self.assertEqual(self.searcher.search("private", mode="name"), [])
        self.assertEqual(len(self.searcher.search("private", mode="name", include_hidden=True)), 1)

    def test_modified_timestamp_filter(self):
        path = self.root / "research" / "facts.txt"
        timestamp = path.stat().st_mtime
        self.assertEqual(len(self.searcher.search("facts", mode="name", modified_after=timestamp - 1)), 1)
        self.assertEqual(self.searcher.search("facts", mode="name", modified_after=timestamp + 10), [])


    def test_search_rejects_symlink_root(self):
        outside = Path(self.tmp.name) / "outside-root"
        outside.mkdir()
        (outside / "needle.txt").write_text("needle", encoding="utf-8")
        link = self.root / "linked-root"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        with self.assertRaises(UnsafePathError):
            self.searcher.search("needle", within="linked-root")
        with self.assertRaises(UnsafePathError):
            self.searcher.search_folders("needle", within="linked-root")

    def test_saved_search_round_trip_and_delete(self):
        self.searcher.save_search("story-files", {"query": "scene", "mode": "name"})
        second = WorkspaceSearch(self.engine)
        self.assertIn("story-files", second.saved_searches())
        self.assertEqual(len(second.run_saved_search("story-files")), 1)
        self.assertTrue(second.delete_saved_search("story-files"))
        self.assertFalse(second.delete_saved_search("story-files"))

    def test_saved_searches_defensively_copy_nested_query_values(self):
        self.searcher.save_search("nested", {"query": "scene", "filters": {"extensions": ["md"]}})
        exported = self.searcher.saved_searches()
        exported["nested"]["filters"]["extensions"].append("exe")
        self.assertEqual(
            self.searcher.saved_searches()["nested"]["filters"]["extensions"], ["md"]
        )

    def test_saved_search_rejects_nonserializable_data(self):
        with self.assertRaises(TypeError):
            self.searcher.save_search("bad", {"value": object()})

    def test_search_ignores_symlink_files_and_directories(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        try:
            (self.root / "secret-link.txt").symlink_to(outside / "secret.txt")
            (self.root / "external-dir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        self.assertEqual(self.searcher.search("secret", mode="content"), [])
        self.assertEqual(self.searcher.search_folders("external"), [])


    def test_invalid_extensions_are_reported_as_validation_errors(self):
        for extensions in ([None], [""], [3], 7):
            with self.subTest(extensions=extensions), self.assertRaises((ValueError, TypeError)):
                self.searcher.search("scene", extensions=extensions)

    def test_non_finite_timestamps_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.searcher.search("scene", modified_after=value)

    def test_non_boolean_flags_are_rejected(self):
        with self.assertRaises(TypeError):
            self.searcher.search("scene", regex="yes")
        with self.assertRaises(TypeError):
            self.searcher.search_folders("scene", case_sensitive=1)

    def test_malformed_saved_search_fails_with_domain_error(self):
        self.searcher.save_search("broken", {"not_query": "scene"})
        with self.assertRaisesRegex(Exception, "invalid query format"):
            self.searcher.run_saved_search("broken")


if __name__ == "__main__":
    unittest.main()
