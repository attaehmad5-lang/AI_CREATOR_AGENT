"""Safe workspace file operations built on FolderEngine path confinement."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Iterable

from projects.folder_engine import FolderEngine, FolderEngineError, UnsafePathError


class FileOperationError(FolderEngineError):
    """Base error for workspace file operations."""


class WorkspaceFileOps:
    """Perform bounded file operations without following symlinks."""

    MAGIC = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"%PDF-", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"ID3", "audio/mpeg"),
        (b"OggS", "audio/ogg"),
        (b"fLaC", "audio/flac"),
        (b"\x1a\x45\xdf\xa3", "video/webm"),
    )

    def __init__(self, engine: FolderEngine, *, max_file_bytes: int = 100 * 1024 * 1024) -> None:
        if not isinstance(engine, FolderEngine):
            raise TypeError("engine must be a FolderEngine")
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        self.engine = engine
        self.max_file_bytes = max_file_bytes

    def _resolve(self, path: str | os.PathLike[str]) -> Path:
        target = self.engine.resolve(path)
        if target == self.engine.root:
            raise UnsafePathError("workspace root is not a file path")
        return target

    def _existing_file(self, path: str | os.PathLike[str]) -> Path:
        target = self._resolve(path)
        if target.is_symlink():
            raise UnsafePathError("file symlinks are not supported")
        if not target.is_file():
            raise FileNotFoundError(f"file does not exist: {target.relative_to(self.engine.root).as_posix()}")
        return target

    def upload_bytes(self, relative_path: str | os.PathLike[str], data: bytes, *, overwrite: bool = False) -> dict:
        """Atomically save bytes under the workspace root with a size limit."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > self.max_file_bytes:
            raise ValueError("file exceeds configured size limit")
        target = self._resolve(relative_path)
        if target.exists() and not overwrite:
            raise FileExistsError(f"destination already exists: {target}")
        if target.exists() and target.is_dir():
            raise IsADirectoryError(str(target))
        self.engine.ensure_root()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".upload-", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink():
                raise UnsafePathError("file symlinks are not supported")
            if overwrite:
                os.replace(temp_name, target)
            else:
                # Hard-link publication is atomic and fails if another writer
                # creates the destination after the initial existence check.
                os.link(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return self.describe(relative_path)

    def download_bytes(self, relative_path: str | os.PathLike[str]) -> bytes:
        target = self._existing_file(relative_path)
        if target.stat().st_size > self.max_file_bytes:
            raise ValueError("file exceeds configured size limit")
        return target.read_bytes()

    def describe(self, relative_path: str | os.PathLike[str]) -> dict:
        target = self._existing_file(relative_path)
        data = target.read_bytes() if target.stat().st_size <= self.max_file_bytes else b""
        detected = next((mime for signature, mime in self.MAGIC if data.startswith(signature)), None)
        return {"path": target.relative_to(self.engine.root).as_posix(),
                "name": target.name, "size": target.stat().st_size,
                "sha256": self.sha256_file(target), "mime_type": detected,
                "extension": target.suffix.lower()}

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def sha256_file(cls, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def rename(self, relative_path: str | os.PathLike[str], new_name: str) -> dict:
        source = self._existing_file(relative_path)
        safe_name = self.engine.validate_name(new_name)
        destination = source.with_name(safe_name)
        if destination.exists():
            raise FileExistsError(str(destination))
        source.rename(destination)
        return self.describe(destination.relative_to(self.engine.root).as_posix())

    def move(self, relative_path: str | os.PathLike[str], destination_path: str | os.PathLike[str]) -> dict:
        source = self._existing_file(relative_path)
        destination = self._resolve(destination_path)
        if destination.exists():
            raise FileExistsError(str(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return self.describe(destination.relative_to(self.engine.root).as_posix())

    def copy(self, relative_path: str | os.PathLike[str], destination_path: str | os.PathLike[str]) -> dict:
        source = self._existing_file(relative_path)
        destination = self._resolve(destination_path)
        if destination.exists():
            raise FileExistsError(str(destination))
        if source.stat().st_size > self.max_file_bytes:
            raise ValueError("file exceeds configured size limit")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Stage the copy beside its destination, then publish atomically. A
        # failed/short read must not leave a truncated file at the final path.
        fd, temporary_name = tempfile.mkstemp(
            prefix=".copy-", suffix=".tmp", dir=str(destination.parent)
        )
        try:
            with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(str(destination))
            # Hard-link publication avoids overwriting a destination created
            # concurrently after the preflight check.
            os.link(temporary_name, destination)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        return self.describe(destination.relative_to(self.engine.root).as_posix())

    def delete(self, relative_path: str | os.PathLike[str]) -> None:
        self._existing_file(relative_path).unlink()

    def bulk_rename(self, mapping: Iterable[tuple[str, str]]) -> list[dict]:
        """Preflight a rename batch before applying any rename."""
        pairs = list(mapping)
        if not pairs:
            return []
        sources = [self._existing_file(source) for source, _ in pairs]
        destinations = []
        for source, (_old, new_name) in zip(sources, pairs):
            destinations.append(source.with_name(self.engine.validate_name(new_name)))
        if len(set(destinations)) != len(destinations):
            raise FileExistsError("bulk rename contains duplicate destinations")
        source_set = set(sources)
        for destination in destinations:
            if destination.exists() and destination not in source_set:
                raise FileExistsError(f"destination already exists: {destination}")
        # Use unique temporary names so swaps such as a.txt <-> b.txt are safe.
        temporary = []
        for source in sources:
            temp = source.with_name(".rename-" + uuid.uuid4().hex)
            source.rename(temp)
            temporary.append(temp)
        committed = []
        try:
            for temp, destination in zip(temporary, destinations):
                temp.rename(destination)
                committed.append((destination, temp))
        except OSError:
            # Restore completed destinations to their staging names first,
            # then restore every staging name to its original path.
            for destination, temp in reversed(committed):
                if destination.exists():
                    destination.rename(temp)
            for temp, source in zip(temporary, sources):
                if temp.exists():
                    temp.rename(source)
            raise
        return [self.describe(destination.relative_to(self.engine.root).as_posix()) for destination in destinations]
