"""Atomic, locked, checksummed JSON storage with backup/restore and crash recovery."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .errors import NotFoundError, StoreCorruptError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class AtomicJsonStore:
    """Single-file JSON store. Writes are atomic (tmp+fsync+replace) and verified by SHA-256."""

    _tlocks: dict = {}
    _tlocks_guard = threading.Lock()

    def __init__(self, path, default: Optional[Callable[[], Any]] = None):
        self.path = Path(path)
        self.default = default or dict
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        key = str(self.path.resolve())
        with self._tlocks_guard:
            self._tlock = self._tlocks.setdefault(key, threading.RLock())

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Interprocess (flock) + intraprocess exclusive lock. Re-entrant per thread."""
        with self._tlock:
            if fcntl is None:  # pragma: no cover
                yield
                return
            with open(self._lock_path, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self) -> Any:
        if not self.path.exists():
            return self.default()
        try:
            envelope = json.loads(self.path.read_text("utf-8"))
            data = envelope["data"]
            expected = envelope["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreCorruptError(f"unreadable store: {self.path.name}") from exc
        if expected != digest(data):
            raise StoreCorruptError(f"checksum mismatch: {self.path.name}")
        return data

    def write(self, data: Any) -> None:
        with self.lock():
            self._write_unlocked(data)

    def _write_unlocked(self, data: Any) -> None:
        payload = json.dumps({"sha256": digest(data), "data": data}, sort_keys=True, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):  # fsync directory so the rename is durable
            dfd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

    def update(self, fn: Callable[[Any], Any]) -> Any:
        """Read-modify-write under an exclusive lock. fn may mutate and/or return new data."""
        with self.lock():
            data = self.read()
            result = fn(data)
            final = data if result is None else result
            self._write_unlocked(final)
            return final

    def backup(self) -> Path:
        with self.lock():
            if not self.path.exists():
                raise NotFoundError("nothing to back up")
            self.read()
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{time.time_ns() % 10**9:09d}"
            dest = self.path.with_name(f"{self.path.name}.bak.{stamp}")
            shutil.copy2(self.path, dest)
            return dest

    def list_backups(self) -> list:
        return sorted(self.path.parent.glob(self.path.name + ".bak.*"))

    def restore(self, backup) -> None:
        backup = Path(backup)
        if not backup.exists():
            raise NotFoundError(f"backup not found: {backup}")
        data = AtomicJsonStore(backup).read()
        self.write(data)

    def recover(self) -> bool:
        """If the live store is corrupt, restore the newest valid backup. True if restored."""
        try:
            self.read()
            return False
        except StoreCorruptError:
            pass
        for candidate in reversed(self.list_backups()):
            try:
                self.restore(candidate)
                return True
            except StoreCorruptError:
                continue
        raise StoreCorruptError("no valid backup available")

    def cleanup_temp(self) -> int:
        """Remove stale temp files left by a crash."""
        n = 0
        for tmp in self.path.parent.glob(self.path.name + ".*.tmp"):
            with contextlib.suppress(OSError):
                tmp.unlink()
                n += 1
        return n
