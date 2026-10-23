"""Tests for workspace file version history and integrity checks."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from concurrent.futures import ThreadPoolExecutor

from projects.folder_engine import FolderEngine, UnsafePathError
from projects.workspace_versioning import VersioningError, WorkspaceVersionStore


class WorkspaceVersionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = FolderEngine(Path(self.temp.name) / "workspace")
        self.engine.ensure_root()
        self.store = WorkspaceVersionStore(self.engine, max_versions_per_file=2)
        self.file = self.engine.root / "notes.txt"
        self.file.write_text("first", encoding="utf-8")

    def test_create_list_restore_version(self):
        first = self.store.create_version("notes.txt", note="before edit")
        self.file.write_text("second", encoding="utf-8")
        second = self.store.create_version("notes.txt")
        self.file.write_text("third", encoding="utf-8")
        result = self.store.restore_version(first["id"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), "first")
        self.assertEqual(result["sha256"], first["sha256"])
        self.assertEqual({v["id"] for v in self.store.list_versions("notes.txt")},
                         {first["id"], second["id"]})

    def test_retention_prunes_oldest_version(self):
        ids = []
        for value in ("a", "b", "c"):
            self.file.write_text(value, encoding="utf-8")
            ids.append(self.store.create_version("notes.txt")["id"])
        self.assertEqual({v["id"] for v in self.store.list_versions("notes.txt")},
                         set(ids[1:]))

    def test_corrupted_blob_is_rejected(self):
        version = self.store.create_version("notes.txt")
        (self.store.blobs / version["blob"]).write_bytes(b"tampered")
        with self.assertRaises(VersioningError):
            self.store.restore_version(version["id"])
        self.assertEqual(self.file.read_text(encoding="utf-8"), "first")

    def test_missing_and_unknown_version_are_reported(self):
        with self.assertRaises(FileNotFoundError):
            self.store.create_version("missing.txt")
        with self.assertRaises(KeyError):
            self.store.restore_version("unknown")

    def test_reserved_store_path_cannot_be_versioned(self):
        with self.assertRaises(UnsafePathError):
            self.store.create_version(".workspace_versions/manifest.json")

    def test_delete_version_and_orphan_blob_cleanup(self):
        one = self.store.create_version("notes.txt")
        self.assertTrue(self.store.delete_version(one["id"]))
        self.assertFalse(self.store.delete_version(one["id"]))
        self.assertEqual(self.store.list_versions("notes.txt"), [])
        self.assertFalse((self.store.blobs / one["blob"]).exists())

    def test_concurrent_version_creation_keeps_manifest_valid(self):
        self.file.write_text("shared payload", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=8) as pool:
            entries = list(pool.map(lambda _: self.store.create_version("notes.txt"), range(16)))
        ids = [entry["id"] for entry in entries]
        self.assertEqual(len(ids), len(set(ids)))
        current = self.store.list_versions("notes.txt")
        self.assertLessEqual(len(current), 2)
        self.assertTrue(all((self.store.blobs / item["blob"]).is_file() for item in current))

    def test_rejects_symlinked_version_store(self):
        outside = Path(self.temp.name) / "outside-store"
        outside.mkdir()
        store_path = self.engine.root / self.store.STORE_NAME
        import shutil
        shutil.rmtree(store_path)
        try:
            store_path.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable on this platform")
        with self.assertRaises(UnsafePathError):
            WorkspaceVersionStore(self.engine)
        self.assertEqual(list(outside.iterdir()), [])

    def test_rejects_symlinked_manifest(self):
        manifest = self.store.manifest_path
        manifest.unlink()
        outside_manifest = Path(self.temp.name) / "outside-manifest.json"
        outside_manifest.write_text('{"schema_version":1,"versions":[]}', encoding="utf-8")
        try:
            manifest.symlink_to(outside_manifest)
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable on this platform")
        with self.assertRaises(UnsafePathError):
            self.store.list_versions("notes.txt")

    def test_manifest_corruption_fails_closed(self):
        (self.store.manifest_path).write_text("{broken", encoding="utf-8")
        with self.assertRaises(VersioningError):
            self.store.list_versions("notes.txt")


if __name__ == "__main__":
    unittest.main()
