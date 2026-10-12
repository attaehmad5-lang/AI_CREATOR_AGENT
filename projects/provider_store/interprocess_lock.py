"""Portable advisory file locking for coordinating processes sharing JSON state files."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def atomic_replace_with_retry(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    """Retry transient Windows sharing violations during same-directory atomic replacement."""
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.01 * (2 ** attempt), 0.15))


@contextmanager
def interprocess_file_lock(lock_path: str | os.PathLike[str]) -> Iterator[None]:
    """Hold an exclusive OS-level lock until the context exits.

    Callers must reload shared state after acquiring this lock and persist changes
    before releasing it. The lock file is intentionally persistent: deleting it
    while another process holds it could create two independently locked inodes.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)

        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
