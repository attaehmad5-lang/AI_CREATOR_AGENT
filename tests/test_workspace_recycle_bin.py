"""Regression tests for workspace recycle-bin safety and lifecycle."""
import json
import tempfile
import unittest
from pathlib import Path

from projects.folder_engine import FolderEngine, UnsafePathError
from projects.workspace_recycle_bin import RecycleBinError, WorkspaceRecycleBin


class WorkspaceRecycleBinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.engine = FolderEngine(self.root)
        self.engine.ensure_folder("docs")
        (self.root / "docs" / "note.txt").write_text("safe", encoding="utf-8")
        self.bin = WorkspaceRecycleBin(self.engine, retention_seconds=10)

    def test_soft_delete_and_restore(self):
        item = self.bin.delete("docs/note.txt")
        self.assertFalse((self.root / "docs" / "note.txt").exists())
        self.assertEqual(self.bin.restore(item["id"]), "docs/note.txt")
        self.assertEqual((self.root / "docs" / "note.txt").read_text(), "safe")

    def test_restore_collision_preserves_trashed_payload(self):
        item = self.bin.delete("docs/note.txt")
        (self.root / "docs" / "note.txt").write_text("new", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.bin.restore(item["id"])
        self.assertEqual(len(self.bin.items()), 1)

    def test_delete_rejects_root_and_traversal(self):
        for path in (".", "../outside"):
            with self.subTest(path=path), self.assertRaises(UnsafePathError):
                self.bin.delete(path)

    def test_expiry_purges_only_expired_items(self):
        item = self.bin.delete("docs/note.txt")
        self.assertEqual(self.bin.purge_expired(now=item["expires_at"] - 1), [])
        self.assertEqual(self.bin.purge_expired(now=item["expires_at"] + 1), [item["id"]])
        self.assertEqual(self.bin.items(), [])

    def test_audit_tracks_delete_restore(self):
        item = self.bin.delete("docs/note.txt")
        self.bin.restore(item["id"])
        self.assertEqual([event["action"] for event in self.bin.audit_log()], ["soft_delete", "restore"])

    def test_missing_item_rejected(self):
        with self.assertRaises(KeyError):
            self.bin.restore("not-a-real-id")

    def test_manifest_rejects_traversal_payload_and_original_path(self):
        self.bin.delete("docs/note.txt")
        manifest = self.bin.state_path
        state = json.loads(manifest.read_text(encoding="utf-8"))
        state["items"][0]["payload"] = "../../outside"
        manifest.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(RecycleBinError):
            self.bin.items()
        state["items"][0]["payload"] = state["items"][0]["id"]
        state["items"][0]["original_path"] = "../outside.txt"
        manifest.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(RecycleBinError):
            self.bin.items()

    def test_manifest_rejects_traversal_item_identifier(self):
        self.bin.delete("docs/note.txt")
        manifest = self.bin.state_path
        state = json.loads(manifest.read_text(encoding="utf-8"))
        state["items"][0]["id"] = "../../outside"
        state["items"][0]["payload"] = "../../outside"
        manifest.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(RecycleBinError):
            self.bin.items()

    def test_rejects_symlinked_trash_directory(self):
        import shutil
        outside = Path(self.tmp.name) / "outside-trash"
        outside.mkdir()
        self.bin.trash.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.bin.trash)
        try:
            self.bin.trash.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable on this platform")
        with self.assertRaises(UnsafePathError):
            self.bin.items()

    def test_bad_retention_rejected(self):
        with self.assertRaises(ValueError):
            WorkspaceRecycleBin(self.engine, retention_seconds=-1)


if __name__ == "__main__":
    unittest.main()
