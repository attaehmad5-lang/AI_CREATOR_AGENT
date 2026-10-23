"""Tests for quota enforcement at the file-operation boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from projects.folder_engine import FolderEngine
from projects.workspace_file_ops import WorkspaceFileOps
from projects.workspace_quota import QuotaError, WorkspaceQuotaManager
from projects.workspace_quota_ops import QuotaAwareWorkspaceFileOps


class QuotaAwareWorkspaceFileOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = FolderEngine(Path(self.temp.name) / "workspace")
        self.engine.ensure_root()
        self.file_ops = WorkspaceFileOps(self.engine)
        self.quota = WorkspaceQuotaManager(byte_limit=8, file_limit=2)
        self.ops = QuotaAwareWorkspaceFileOps(self.file_ops, self.quota)

    def test_upload_reserves_and_commits_actual_usage(self):
        info = self.ops.upload_bytes("a.txt", b"hello")
        self.assertEqual(info["size"], 5)
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (5, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_quota_rejects_upload_before_filesystem_write(self):
        with self.assertRaises(QuotaError):
            self.ops.upload_bytes("too-large.txt", b"123456789")
        self.assertFalse((self.engine.root / "too-large.txt").exists())
        self.assertEqual(self.quota.snapshot().bytes_reserved, 0)

    def test_failed_upload_releases_reservation(self):
        with self.assertRaises(FileExistsError):
            self.ops.upload_bytes("same.txt", b"a")
            self.ops.upload_bytes("same.txt", b"b")
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (1, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_upload_commit_failure_removes_published_file(self):
        with patch.object(self.quota, "commit", side_effect=QuotaError("injected commit failure")):
            with self.assertRaisesRegex(QuotaError, "injected commit failure"):
                self.ops.upload_bytes("a.txt", b"hello")
        self.assertFalse((self.engine.root / "a.txt").exists())
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (0, 0))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_overwrite_commit_failure_restores_previous_file(self):
        self.ops.upload_bytes("a.txt", b"old")
        with patch.object(self.quota, "commit", side_effect=QuotaError("injected commit failure")):
            with self.assertRaisesRegex(QuotaError, "injected commit failure"):
                self.ops.upload_bytes("a.txt", b"newer", overwrite=True)
        self.assertEqual(self.file_ops.download_bytes("a.txt"), b"old")
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (3, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_copy_commit_failure_removes_published_copy(self):
        self.ops.upload_bytes("a.txt", b"abc")
        with patch.object(self.quota, "commit", side_effect=QuotaError("injected commit failure")):
            with self.assertRaisesRegex(QuotaError, "injected commit failure"):
                self.ops.copy("a.txt", "b.txt")
        self.assertFalse((self.engine.root / "b.txt").exists())
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (3, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_shrinking_overwrite_releases_the_byte_difference(self):
        self.ops.upload_bytes("a.txt", b"123456")
        self.ops.upload_bytes("a.txt", b"xy", overwrite=True)
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (2, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_growing_overwrite_counts_only_the_byte_difference(self):
        self.ops.upload_bytes("a.txt", b"abc")
        self.ops.upload_bytes("a.txt", b"1234567", overwrite=True)
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (7, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_shrinking_overwrite_restores_file_if_quota_decrement_fails(self):
        self.ops.upload_bytes("a.txt", b"123456")
        with patch.object(self.quota, "record_delete",
                          side_effect=QuotaError("injected decrement failure")):
            with self.assertRaisesRegex(QuotaError, "injected decrement failure"):
                self.ops.upload_bytes("a.txt", b"x", overwrite=True)
        self.assertEqual(self.file_ops.download_bytes("a.txt"), b"123456")
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (6, 1))
        self.assertEqual((snap.bytes_reserved, snap.files_reserved), (0, 0))

    def test_delete_restores_file_if_quota_decrement_fails(self):
        self.ops.upload_bytes("a.txt", b"preserve me")
        with patch.object(self.quota, "record_delete",
                          side_effect=QuotaError("injected decrement failure")):
            with self.assertRaisesRegex(QuotaError, "injected decrement failure"):
                self.ops.delete("a.txt")
        self.assertEqual(self.file_ops.download_bytes("a.txt"), b"preserve me")
        snap = self.quota.snapshot()
        self.assertEqual((snap.bytes_used, snap.files_used), (11, 1))

    def test_copy_and_delete_keep_accounting_consistent(self):
        self.ops.upload_bytes("a.txt", b"abc")
        self.ops.copy("a.txt", "b.txt")
        self.assertEqual((self.quota.snapshot().bytes_used, self.quota.snapshot().files_used), (6, 2))
        self.ops.delete("b.txt")
        self.assertEqual((self.quota.snapshot().bytes_used, self.quota.snapshot().files_used), (3, 1))

    def test_delete_refuses_when_usage_is_not_reconciled(self):
        (self.engine.root / "untracked.txt").write_bytes(b"external")
        with self.assertRaises(ValueError):
            self.ops.delete("untracked.txt")
        self.assertTrue((self.engine.root / "untracked.txt").exists())


if __name__ == "__main__":
    unittest.main()
