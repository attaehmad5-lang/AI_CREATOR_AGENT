"""Safe, portable core folder engine for AI_CREATOR_AGENT workspaces.

This module provides the foundational folder primitives only. It intentionally
avoids import-time filesystem changes and keeps operations confined to a
caller-selected workspace root.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class FolderEngineError(Exception):
    """Base error for folder-engine operations."""


class UnsafePathError(FolderEngineError, ValueError):
    """Raised when a path is invalid or escapes the workspace root."""


class FolderTypeError(FolderEngineError, NotADirectoryError):
    """Raised when a requested directory path is occupied by a file."""


@dataclass(frozen=True)
class FolderEntry:
    """A stable, root-relative directory description."""

    name: str
    relative_path: str
    absolute_path: Path
    exists: bool
    is_directory: bool


class FolderEngine:
    """Create, inspect, and enumerate directories safely beneath a root."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise TypeError("root must be a path-like value")
        raw = os.fspath(root)
        if not raw or not raw.strip():
            raise ValueError("workspace root must not be empty")
        self.root = Path(raw).expanduser().resolve()

    @staticmethod
    def validate_name(name: str) -> str:
        """Validate one portable directory name, not a path."""
        if not isinstance(name, str):
            raise TypeError("folder name must be a string")
        normalized = name.strip()
        if not normalized or normalized in {".", ".."}:
            raise UnsafePathError("folder name must be a non-empty name")
        if normalized != name:
            raise UnsafePathError("leading or trailing whitespace is not allowed")
        if any(ord(ch) < 32 for ch in normalized):
            raise UnsafePathError("control characters are not allowed in folder names")
        if any(ch in normalized for ch in ("/", "\\\\", "\\0")):
            raise UnsafePathError("folder name must not contain path separators")
        if re.match(r"^[A-Za-z]:", normalized):
            raise UnsafePathError("drive-qualified names are not allowed")
        return normalized

    def resolve(self, relative_path: str | os.PathLike[str]) -> Path:
        """Resolve a relative path and reject absolute paths or root escape."""
        raw = os.fspath(relative_path)
        if not isinstance(raw, str):
            raise TypeError("relative path must be a string path")
        if not raw or not raw.strip():
            raise UnsafePathError("relative path must not be empty")
        if "\\" in raw or any(ord(ch) < 32 for ch in raw):
            raise UnsafePathError("non-portable path separator or control character")
        if re.match(r"^[A-Za-z]:", raw):
            raise UnsafePathError("drive-qualified paths are not allowed")
        candidate = Path(raw)
        if candidate.is_absolute() or candidate.drive:
            raise UnsafePathError("absolute paths are not allowed")
        if any(part == ".." for part in candidate.parts):
            raise UnsafePathError("parent traversal is not allowed")
        target = (self.root / candidate).resolve()
        if target != self.root and self.root not in target.parents:
            raise UnsafePathError("path escapes workspace root")
        return target

    def ensure_root(self) -> Path:
        """Create the root if needed and verify that it is a directory."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FolderTypeError(f"workspace root is not a directory: {self.root}")
        return self.root

    def ensure_folder(self, relative_path: str | os.PathLike[str]) -> FolderEntry:
        """Create one folder, including parents, and return its description."""
        target = self.resolve(relative_path)
        if target == self.root:
            raise UnsafePathError("workspace root cannot be created as a child folder")
        self.ensure_root()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise FolderTypeError(f"path is occupied by a file: {target}") from exc
        if not target.is_dir():
            raise FolderTypeError(f"path is not a directory: {target}")
        return self.describe(relative_path)

    def ensure_folders(self, paths: Iterable[str | os.PathLike[str]]) -> list[FolderEntry]:
        """Create multiple folders in input order; repeated paths are safe."""
        if isinstance(paths, (str, bytes, os.PathLike)):
            raise TypeError("paths must be an iterable of paths, not a single path")
        return [self.ensure_folder(path) for path in paths]

    def describe(self, relative_path: str | os.PathLike[str]) -> FolderEntry:
        """Describe a path without creating it."""
        target = self.resolve(relative_path)
        exists = target.exists()
        is_directory = target.is_dir() if exists else False
        return FolderEntry(
            name=target.name,
            relative_path=target.relative_to(self.root).as_posix(),
            absolute_path=target,
            exists=exists,
            is_directory=is_directory,
        )

    def list_folders(self, relative_path: str | os.PathLike[str] = ".") -> list[FolderEntry]:
        """List immediate child directories in deterministic name order."""
        parent = self.root if os.fspath(relative_path) == "." else self.resolve(relative_path)
        if not parent.exists():
            return []
        if not parent.is_dir():
            raise FolderTypeError(f"path is not a directory: {parent}")
        entries = []
        for child in sorted(parent.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
            if child.is_dir():
                entries.append(self.describe(child.relative_to(self.root)))
        return entries
