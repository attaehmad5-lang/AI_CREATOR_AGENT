"""Tests for quota enforcement at the file-operation boundary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
        self.quota = WorkspaceQuotaManager(byte_limit=8, file_limit=2)
        self.ops = QuotaAwareWorkspaceFileOps(WorkspaceFileOps(self.engine), self.quota)

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
