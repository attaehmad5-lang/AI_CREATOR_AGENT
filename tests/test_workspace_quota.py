"""Tests for workspace quota accounting and reservation safety."""
import unittest
from projects.workspace_quota import QuotaError, WorkspaceQuotaManager


class WorkspaceQuotaTests(unittest.TestCase):
    def test_reservation_counts_against_limit_before_write(self):
        quota = WorkspaceQuotaManager(byte_limit=100, file_limit=2)
        quota.reserve("upload-1", bytes_count=60, files_count=1)
        with self.assertRaises(QuotaError):
            quota.reserve("upload-2", bytes_count=50, files_count=1)
        self.assertEqual(quota.snapshot().bytes_reserved, 60)

    def test_commit_uses_actual_usage_and_releases_reservation(self):
        quota = WorkspaceQuotaManager(byte_limit=100, file_limit=3)
        quota.reserve("job", bytes_count=50, files_count=2)
        state = quota.commit("job", actual_bytes=40, actual_files=1)
        self.assertEqual((state.bytes_used, state.files_used), (40, 1))
        self.assertEqual((state.bytes_reserved, state.files_reserved), (0, 0))

    def test_failed_commit_keeps_reservation_for_retry_or_release(self):
        quota = WorkspaceQuotaManager(byte_limit=100)
        quota.reserve("job", bytes_count=60)
        with self.assertRaises(QuotaError):
            quota.commit("job", actual_bytes=101)
        self.assertEqual(quota.snapshot().bytes_reserved, 60)
        self.assertTrue(quota.release("job"))

    def test_commit_cannot_oversubscribe_other_byte_reservations(self):
        quota = WorkspaceQuotaManager(byte_limit=100)
        quota.reserve("first", bytes_count=40)
        quota.reserve("second", bytes_count=40)
        with self.assertRaisesRegex(QuotaError, "other reservations"):
            quota.commit("first", actual_bytes=70)
        state = quota.snapshot()
        self.assertEqual((state.bytes_used, state.bytes_reserved), (0, 80))
        quota.commit("first", actual_bytes=60)
        state = quota.snapshot()
        self.assertEqual((state.bytes_used, state.bytes_reserved), (60, 40))

    def test_commit_cannot_oversubscribe_other_file_reservations(self):
        quota = WorkspaceQuotaManager(file_limit=2)
        quota.reserve("first", files_count=1)
        quota.reserve("second", files_count=1)
        with self.assertRaisesRegex(QuotaError, "other reservations"):
            quota.commit("first", actual_files=2)
        state = quota.snapshot()
        self.assertEqual((state.files_used, state.files_reserved), (0, 2))
        quota.commit("first", actual_files=1)
        state = quota.snapshot()
        self.assertEqual((state.files_used, state.files_reserved), (1, 1))

    def test_release_is_idempotently_reported(self):
        quota = WorkspaceQuotaManager(byte_limit=20)
        quota.reserve("job", bytes_count=10)
        self.assertTrue(quota.release("job"))
        self.assertFalse(quota.release("job"))

    def test_reconcile_and_delete_accounting(self):
        quota = WorkspaceQuotaManager(byte_limit=200, file_limit=5)
        quota.reconcile(bytes_used=80, files_used=2)
        state = quota.record_delete(bytes_count=20, files_count=1)
        self.assertEqual((state.bytes_used, state.files_used), (60, 1))

    def test_invalid_limits_and_usage_are_rejected(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(QuotaError):
                WorkspaceQuotaManager(byte_limit=value)
        quota = WorkspaceQuotaManager()
        with self.assertRaises(QuotaError):
            quota.reserve("bad", bytes_count=-1)

    def test_duplicate_reservation_and_unknown_commit(self):
        quota = WorkspaceQuotaManager()
        quota.reserve("job", bytes_count=1)
        with self.assertRaises(QuotaError):
            quota.reserve("job", bytes_count=1)
        with self.assertRaises(KeyError):
            quota.commit("missing")

    def test_lowering_limit_below_usage_is_rejected(self):
        quota = WorkspaceQuotaManager(byte_limit=100)
        quota.reconcile(bytes_used=60, files_used=0)
        with self.assertRaises(QuotaError):
            quota.set_limits(byte_limit=50)


if __name__ == "__main__":
    unittest.main()
