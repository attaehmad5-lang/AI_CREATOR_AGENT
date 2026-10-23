"""Quota-enforced facade for workspace file writes and deletes.

Use one WorkspaceQuotaManager per workspace. The manager is process-local; a
multi-process deployment must replace it with shared transactional accounting.
"""
from __future__ import annotations

import uuid
from typing import Any

from projects.workspace_file_ops import WorkspaceFileOps
from projects.workspace_quota import WorkspaceQuotaManager


class QuotaAwareWorkspaceFileOps:
    """Apply quota reservations around common file operations."""

    def __init__(self, file_ops: WorkspaceFileOps, quota: WorkspaceQuotaManager) -> None:
        if not isinstance(file_ops, WorkspaceFileOps):
            raise TypeError("file_ops must be a WorkspaceFileOps")
        if not isinstance(quota, WorkspaceQuotaManager):
            raise TypeError("quota must be a WorkspaceQuotaManager")
        self.file_ops = file_ops
        self.quota = quota

    def upload_bytes(self, relative_path: str, data: bytes, *, overwrite: bool = False) -> dict[str, Any]:
        target = self.file_ops.engine.resolve(relative_path)
        if overwrite and target.exists():
            old_data = self.file_ops.download_bytes(relative_path)
            old_size = len(old_data)
            tracked = self.quota.snapshot()
            if old_size and (tracked.bytes_used < old_size or tracked.files_used < 1):
                raise ValueError("quota usage is not reconciled with the workspace")
            reservation_id = uuid.uuid4().hex
            self.quota.reserve(reservation_id, bytes_count=max(0, len(data) - old_size),
                               files_count=0)
            published = False
            try:
                result = self.file_ops.upload_bytes(relative_path, data, overwrite=True)
                published = True
                try:
                    self.quota.commit(reservation_id, actual_bytes=max(0, len(data) - old_size),
                                      actual_files=0)
                except Exception:
                    self.file_ops.upload_bytes(relative_path, old_data, overwrite=True)
                    published = False
                    raise
                if len(data) < old_size:
                    self.quota.record_delete(bytes_count=old_size - len(data), files_count=0)
                return result
            except Exception:
                self.quota.release(reservation_id)
                if published:
                    # A post-commit accounting failure is not safe to undo here:
                    # usage may already have been committed. Leave evidence on disk.
                    pass
                raise

        reservation_id = uuid.uuid4().hex
        self.quota.reserve(reservation_id, bytes_count=len(data), files_count=1)
        published = False
        try:
            result = self.file_ops.upload_bytes(relative_path, data, overwrite=False)
            published = True
            try:
                self.quota.commit(reservation_id, actual_bytes=result["size"], actual_files=1)
            except Exception:
                self.file_ops.delete(relative_path)
                published = False
                raise
            return result
        except Exception:
            self.quota.release(reservation_id)
            raise

    def copy(self, source: str, destination: str) -> dict[str, Any]:
        info = self.file_ops.describe(source)
        reservation_id = uuid.uuid4().hex
        self.quota.reserve(reservation_id, bytes_count=info["size"], files_count=1)
        try:
            result = self.file_ops.copy(source, destination)
            try:
                self.quota.commit(reservation_id, actual_bytes=result["size"], actual_files=1)
            except Exception:
                self.file_ops.delete(destination)
                raise
            return result
        except Exception:
            self.quota.release(reservation_id)
            raise

    def delete(self, relative_path: str) -> None:
        info = self.file_ops.describe(relative_path)
        snapshot = self.quota.snapshot()
        if info["size"] > snapshot.bytes_used or snapshot.files_used < 1:
            raise ValueError("quota usage is not reconciled with the workspace")
        self.file_ops.delete(relative_path)
        self.quota.record_delete(bytes_count=info["size"], files_count=1)
