"""Safe, portable core folder engine for AI_CREATOR_AGENT workspaces.

This module provides the foundational folder primitives only. It intentionally
avoids import-time filesystem changes and keeps operations confined to a
caller-selected workspace root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
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
class FolderTreeNode:
    """A directory node with deterministic, recursively nested children."""

    entry: "FolderEntry"
    children: tuple["FolderTreeNode", ...] = ()


@dataclass(frozen=True)
class FolderEntry:
    """A stable, root-relative directory description."""

    name: str
    relative_path: str
    absolute_path: Path
    exists: bool
    is_directory: bool


@dataclass(frozen=True)
class StructureValidation:
    """Immutable result of checking a required folder layout."""

    valid: bool
    checked_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    non_directory_paths: tuple[str, ...]


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
        if any(ch in normalized for ch in ("/", "\\", "\0")):
            raise UnsafePathError("folder name must not contain path separators")
        if any(ch in normalized for ch in '<>:"|?*'):
            raise UnsafePathError("folder name contains a character not portable across platforms")
        if normalized.endswith("."):
            raise UnsafePathError("folder names must not end with a dot")
        if re.match(r"^[A-Za-z]:", normalized):
            raise UnsafePathError("drive-qualified names are not allowed")
        # Windows reserves these device names even when an extension is added.
        device_stem = normalized.split(".", 1)[0].casefold()
        if device_stem in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[1-9]", device_stem):
            raise UnsafePathError("reserved device names are not portable folder names")
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
        for part in candidate.parts:
            self.validate_name(part)
        target = (self.root / candidate).resolve()
        if target != self.root and self.root not in target.parents:
            raise UnsafePathError("path escapes workspace root")
        return target

    def ensure_root(self) -> Path:
        """Create the root if needed and verify that it is a directory."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise FolderTypeError(f"workspace root is occupied by a file: {self.root}") from exc
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
        requested = list(paths)
        # Validate the complete batch before making filesystem changes, so an
        # unsafe later entry cannot leave earlier folders partially created.
        targets: list[Path] = []
        for path in requested:
            target = self.resolve(path)
            if target == self.root:
                raise UnsafePathError("workspace root cannot be created as a child folder")
            targets.append(target)

        # Also detect known file collisions before creating any requested path.
        # This avoids partial batches when a later target or one of its parents
        # is already occupied by a regular file.
        for target in targets:
            current = target
            while current != self.root:
                if current.exists() and not current.is_dir():
                    raise FolderTypeError(f"path is occupied by a file: {current}")
                current = current.parent
            if self.root.exists() and not self.root.is_dir():
                raise FolderTypeError(f"workspace root is occupied by a file: {self.root}")
        return [self.ensure_folder(path) for path in requested]

    def _metadata_path(self) -> Path:
        return self.root / ".ai_creator_agent_folder_metadata.json"

    def _read_metadata(self) -> dict:
        path = self._metadata_path()
        if not path.exists():
            return {"version": 1, "folders": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FolderEngineError(f"folder metadata is unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("folders"), dict):
            raise FolderEngineError("folder metadata has an unsupported or invalid format")
        return data

    def _write_metadata(self, data: dict) -> None:
        self.ensure_root()
        path = self._metadata_path()
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get_folder_id(self, relative_path: str | os.PathLike[str], *, create: bool = True) -> str | None:
        """Return a stable UUID for a folder; optionally persist one on first access."""
        target = self.resolve(relative_path)
        if target == self.root:
            raise FolderTypeError("folder ID requires an existing child directory")
        if not target.exists() and not create:
            return None
        if not target.is_dir():
            raise FolderTypeError("folder ID requires an existing child directory")
        data = self._read_metadata()
        key = target.relative_to(self.root).as_posix()
        record = data["folders"].get(key)
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            return record["id"]
        if not create:
            return None
        folder_id = str(uuid.uuid4())
        data["folders"][key] = {"id": folder_id, "metadata": {}}
        self._write_metadata(data)
        return folder_id

    def get_metadata(self, relative_path: str | os.PathLike[str]) -> dict:
        """Return a defensive copy of user metadata for an existing folder."""
        target = self.resolve(relative_path)
        if target == self.root or not target.is_dir():
            raise FolderTypeError("metadata requires an existing child directory")
        record = self._read_metadata()["folders"].get(target.relative_to(self.root).as_posix(), {})
        metadata = record.get("metadata", {}) if isinstance(record, dict) else {}
        return dict(metadata) if isinstance(metadata, dict) else {}

    def set_metadata(self, relative_path: str | os.PathLike[str], key: str, value: object) -> None:
        """Persist JSON-compatible metadata for a folder using atomic replacement."""
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("metadata key must be a non-empty trimmed string")
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TypeError("metadata value must be JSON serializable") from exc
        target = self.resolve(relative_path)
        if target == self.root or not target.is_dir():
            raise FolderTypeError("metadata requires an existing child directory")
        data = self._read_metadata()
        path_key = target.relative_to(self.root).as_posix()
        record = data["folders"].setdefault(path_key, {"id": str(uuid.uuid4()), "metadata": {}})
        if not isinstance(record, dict) or not isinstance(record.get("metadata", {}), dict):
            raise FolderEngineError("folder metadata record is invalid")
        record.setdefault("metadata", {})[key] = value
        self._write_metadata(data)

    def rename_folder(self, relative_path: str | os.PathLike[str], new_name: str) -> FolderEntry:
        """Rename a folder in place; never overwrite an existing destination."""
        source = self.resolve(relative_path)
        if source == self.root or not source.is_dir():
            raise FolderTypeError("rename requires an existing child directory")
        safe_name = self.validate_name(new_name)
        destination = source.with_name(safe_name)
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        destination.relative_to(self.root)
        source.rename(destination)
        data = self._read_metadata()
        old_key = source.relative_to(self.root).as_posix()
        new_key = destination.relative_to(self.root).as_posix()
        renamed = {}
        for key, value in list(data["folders"].items()):
            if key == old_key or key.startswith(old_key + "/"):
                renamed[new_key + key[len(old_key):]] = data["folders"].pop(key)
        if renamed:
            data["folders"].update(renamed)
            self._write_metadata(data)
        return self.describe(new_key)

    def move_folder(self, relative_path: str | os.PathLike[str], destination_path: str | os.PathLike[str]) -> FolderEntry:
        """Move a folder beneath the workspace, rejecting self/descendant moves."""
        source = self.resolve(relative_path)
        destination = self.resolve(destination_path)
        if source == self.root or not source.is_dir():
            raise FolderTypeError("move requires an existing child directory")
        if destination == source or source in destination.parents:
            raise UnsafePathError("a folder cannot be moved into itself or its descendants")
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.relative_to(self.root)
        old_key = source.relative_to(self.root).as_posix()
        source.rename(destination)
        data = self._read_metadata()
        moved = {}
        for key, value in list(data["folders"].items()):
            if key == old_key or key.startswith(old_key + "/"):
                moved[destination.relative_to(self.root).as_posix() + key[len(old_key):]] = data["folders"].pop(key)
        if moved:
            data["folders"].update(moved)
            self._write_metadata(data)
        return self.describe(destination.relative_to(self.root).as_posix())

    def copy_folder(self, relative_path: str | os.PathLike[str], destination_path: str | os.PathLike[str]) -> FolderEntry:
        """Copy a folder tree without retaining symlinks or overwriting a destination.

        Folder metadata is copied with fresh IDs so the copy is independent of
        the original while retaining descriptive metadata values.
        """
        source = self.resolve(relative_path)
        destination = self.resolve(destination_path)
        if source == self.root or not source.is_dir():
            raise FolderTypeError("copy requires an existing child directory")
        if destination == source or source in destination.parents:
            raise UnsafePathError("a folder cannot be copied into itself or its descendants")
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
        # Do not retain symlinks in the copied tree; they could point outside
        # the workspace or change meaning on another platform.
        for current, dirs, files in os.walk(destination, topdown=True, followlinks=False):
            for name in list(dirs):
                candidate = Path(current) / name
                if candidate.is_symlink():
                    candidate.unlink()
                    dirs.remove(name)
            for name in files:
                candidate = Path(current) / name
                if candidate.is_symlink():
                    candidate.unlink()

        data = self._read_metadata()
        source_key = source.relative_to(self.root).as_posix()
        destination_key = destination.relative_to(self.root).as_posix()
        copied_records = {}
        for key, record in data["folders"].items():
            if key == source_key or key.startswith(source_key + "/"):
                suffix = key[len(source_key):]
                metadata = record.get("metadata", {}) if isinstance(record, dict) else {}
                copied_records[destination_key + suffix] = {
                    "id": str(uuid.uuid4()),
                    "metadata": dict(metadata) if isinstance(metadata, dict) else {},
                }
        if copied_records:
            data["folders"].update(copied_records)
            self._write_metadata(data)
        return self.describe(destination_key)

    def delete_folder(self, relative_path: str | os.PathLike[str], *, recursive: bool = False) -> None:
        """Delete a child folder; non-empty deletion requires explicit recursive=True."""
        target = self.resolve(relative_path)
        if target == self.root or not target.is_dir():
            raise FolderTypeError("delete requires an existing child directory")
        if target.is_symlink():
            raise UnsafePathError("deleting directory symlinks is not supported")
        if recursive:
            shutil.rmtree(target)
        else:
            target.rmdir()
        key = target.relative_to(self.root).as_posix()
        data = self._read_metadata()
        removed = [item for item in data["folders"] if item == key or item.startswith(key + "/")]
        for item in removed:
            data["folders"].pop(item, None)
        if removed:
            self._write_metadata(data)

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

    def validate_structure(
        self, required_paths: Iterable[str | os.PathLike[str]]
    ) -> StructureValidation:
        """Check a required folder layout without creating or modifying files.

        Missing folders and paths occupied by files are reported separately.
        Unsafe paths raise :class:`UnsafePathError` instead of being hidden in
        the report, so callers cannot mistake invalid input for a valid layout.
        """
        if isinstance(required_paths, (str, bytes, os.PathLike)):
            raise TypeError("required_paths must be an iterable of relative paths")
        checked: list[str] = []
        missing: list[str] = []
        non_directories: list[str] = []
        for raw_path in required_paths:
            target = self.resolve(raw_path)
            if target == self.root:
                raise UnsafePathError("workspace root cannot be a required child folder")
            relative = target.relative_to(self.root).as_posix()
            checked.append(relative)
            if not target.exists():
                missing.append(relative)
            elif not target.is_dir():
                non_directories.append(relative)
        return StructureValidation(
            valid=not missing and not non_directories,
            checked_paths=tuple(checked),
            missing_paths=tuple(missing),
            non_directory_paths=tuple(non_directories),
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
            # Directory symlinks are intentionally excluded: following them
            # can expose external trees or make listings platform-dependent.
            if child.is_symlink() or not child.is_dir():
                continue
            entries.append(self.describe(child.relative_to(self.root).as_posix()))
        return entries

    def list_tree(
        self,
        relative_path: str | os.PathLike[str] = ".",
        *,
        max_depth: int | None = None,
    ) -> tuple[FolderTreeNode, ...]:
        """Return a deterministic directory tree rooted at a workspace path.

        Depth is measured from the requested root: 0 includes only the root
        node, 1 includes its immediate child directories, and so on. None
        traverses the complete tree. Symbolic links are not followed, avoiding
        cycles and preventing linked directories from escaping the workspace.
        """
        if max_depth is not None:
            if isinstance(max_depth, bool) or not isinstance(max_depth, int):
                raise TypeError("max_depth must be a non-negative integer or None")
            if max_depth < 0:
                raise ValueError("max_depth must be non-negative")

        raw = os.fspath(relative_path)
        parent = self.root if raw == "." else self.resolve(relative_path)
        if not parent.exists():
            return ()
        if not parent.is_dir():
            raise FolderTypeError(f"path is not a directory: {parent}")

        def build(directory: Path, depth: int) -> FolderTreeNode:
            entry = self.describe(".") if directory == self.root else self.describe(
                directory.relative_to(self.root).as_posix()
            )
            if max_depth is not None and depth >= max_depth:
                return FolderTreeNode(entry=entry)
            children = []
            for child in sorted(
                directory.iterdir(), key=lambda item: (item.name.casefold(), item.name)
            ):
                if child.is_symlink() or not child.is_dir():
                    continue
                children.append(build(child, depth + 1))
            return FolderTreeNode(entry=entry, children=tuple(children))
        
        return (build(parent, 0),)
