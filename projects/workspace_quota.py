"""Thread-safe workspace quota accounting for Project Structure / Folders.

The manager provides atomic reservations for callers to enforce before writes.
Persist reservations externally for multi-process deployments; this implementation
is intentionally process-local and does not claim distributed consistency.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


class QuotaError(ValueError):
    """Raised when a quota configuration or accounting operation is invalid."""


@dataclass(frozen=True)
class QuotaSnapshot:
    byte_limit: int | None
    file_limit: int | None
    bytes_used: int
    files_used: int
    bytes_reserved: int
    files_reserved: int

    @property
    def bytes_available(self) -> int | None:
        return None if self.byte_limit is None else max(
            0, self.byte_limit - self.bytes_used - self.bytes_reserved
        )

    @property
    def files_available(self) -> int | None:
        return None if self.file_limit is None else max(
            0, self.file_limit - self.files_used - self.files_reserved
        )


class WorkspaceQuotaManager:
    """Manage per-workspace byte/file limits and temporary write reservations."""

    def __init__(self, *, byte_limit: int | None = None,
                 file_limit: int | None = None) -> None:
        self._validate_limit("byte_limit", byte_limit)
        self._validate_limit("file_limit", file_limit)
        self.byte_limit = byte_limit
        self.file_limit = file_limit
        self._bytes_used = 0
        self._files_used = 0
        self._bytes_reserved = 0
        self._files_reserved = 0
        self._reservations: dict[str, tuple[int, int]] = {}
        self._lock = RLock()

    @staticmethod
    def _validate_limit(name: str, value: int | None) -> None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise QuotaError(f"{name} must be a non-negative integer or None")

    @staticmethod
    def _validate_amount(name: str, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QuotaError(f"{name} must be a non-negative integer")

    def set_limits(self, *, byte_limit: int | None = None,
                   file_limit: int | None = None) -> None:
        self._validate_limit("byte_limit", byte_limit)
        self._validate_limit("file_limit", file_limit)
        with self._lock:
            if byte_limit is not None and byte_limit < self._bytes_used + self._bytes_reserved:
                raise QuotaError("byte limit is below current usage plus reservations")
            if file_limit is not None and file_limit < self._files_used + self._files_reserved:
                raise QuotaError("file limit is below current usage plus reservations")
            self.byte_limit, self.file_limit = byte_limit, file_limit

    def reconcile(self, *, bytes_used: int, files_used: int) -> QuotaSnapshot:
        """Replace usage from a trusted filesystem scan, retaining live reservations."""
        self._validate_amount("bytes_used", bytes_used)
        self._validate_amount("files_used", files_used)
        with self._lock:
            if self.byte_limit is not None and bytes_used + self._bytes_reserved > self.byte_limit:
                raise QuotaError("reconciled byte usage exceeds limit")
            if self.file_limit is not None and files_used + self._files_reserved > self.file_limit:
                raise QuotaError("reconciled file usage exceeds limit")
            self._bytes_used, self._files_used = bytes_used, files_used
            return self.snapshot()

    def reserve(self, reservation_id: str, *, bytes_count: int = 0,
                files_count: int = 0) -> QuotaSnapshot:
        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise QuotaError("reservation_id must be non-empty")
        self._validate_amount("bytes_count", bytes_count)
        self._validate_amount("files_count", files_count)
        with self._lock:
            if reservation_id in self._reservations:
                raise QuotaError("reservation_id already exists")
            if self.byte_limit is not None and (
                self._bytes_used + self._bytes_reserved + bytes_count > self.byte_limit
            ):
                raise QuotaError("workspace byte quota exceeded")
            if self.file_limit is not None and (
                self._files_used + self._files_reserved + files_count > self.file_limit
            ):
                raise QuotaError("workspace file quota exceeded")
            self._reservations[reservation_id] = (bytes_count, files_count)
            self._bytes_reserved += bytes_count
            self._files_reserved += files_count
            return self.snapshot()

    def commit(self, reservation_id: str, *, actual_bytes: int | None = None,
               actual_files: int | None = None) -> QuotaSnapshot:
        with self._lock:
            if reservation_id not in self._reservations:
                raise KeyError(reservation_id)
            reserved_bytes, reserved_files = self._reservations[reservation_id]
            final_bytes = reserved_bytes if actual_bytes is None else actual_bytes
            final_files = reserved_files if actual_files is None else actual_files
            self._validate_amount("actual_bytes", final_bytes)
            self._validate_amount("actual_files", final_files)
            next_bytes = self._bytes_used + final_bytes
            next_files = self._files_used + final_files
            if self.byte_limit is not None and next_bytes > self.byte_limit:
                raise QuotaError("actual bytes exceed workspace quota")
            if self.file_limit is not None and next_files > self.file_limit:
                raise QuotaError("actual files exceed workspace quota")
            self._bytes_reserved -= reserved_bytes
            self._files_reserved -= reserved_files
            self._bytes_used, self._files_used = next_bytes, next_files
            del self._reservations[reservation_id]
            return self.snapshot()

    def release(self, reservation_id: str) -> bool:
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return False
            self._bytes_reserved -= reservation[0]
            self._files_reserved -= reservation[1]
            return True

    def record_delete(self, *, bytes_count: int, files_count: int = 1) -> QuotaSnapshot:
        self._validate_amount("bytes_count", bytes_count)
        self._validate_amount("files_count", files_count)
        with self._lock:
            if bytes_count > self._bytes_used or files_count > self._files_used:
                raise QuotaError("deletion exceeds tracked usage")
            self._bytes_used -= bytes_count
            self._files_used -= files_count
            return self.snapshot()

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            return QuotaSnapshot(
                self.byte_limit, self.file_limit, self._bytes_used, self._files_used,
                self._bytes_reserved, self._files_reserved,
            )
