"""Bounded, symlink-safe workspace search with reusable saved queries."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from projects.folder_engine import FolderEngine, FolderEngineError, UnsafePathError


class WorkspaceSearch:
    """Search folder names and text files without leaving a workspace root."""

    TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".xml", ".html", ".htm",
                       ".yaml", ".yml", ".log", ".srt", ".vtt", ".fountain", ".py"}

    def __init__(self, engine: FolderEngine, *, max_file_bytes: int = 2 * 1024 * 1024,
                 max_results: int = 500, state_path: str | os.PathLike[str] | None = None) -> None:
        if not isinstance(engine, FolderEngine):
            raise TypeError("engine must be a FolderEngine")
        for name, value in (("max_file_bytes", max_file_bytes), ("max_results", max_results)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.engine = engine
        self.max_file_bytes = max_file_bytes
        self.max_results = max_results
        self.state_path = Path(state_path) if state_path is not None else engine.root / ".workspace_saved_searches.json"

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.engine.root).as_posix()

    def _files(self, root: Path):
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted([name for name in dirs if not (current_path / name).is_symlink()],
                             key=lambda value: (value.casefold(), value))
            for name in sorted(files, key=lambda value: (value.casefold(), value)):
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                yield path

    def search(self, query: str, *, within: str = ".", mode: str = "name",
               extensions: Iterable[str] | None = None, case_sensitive: bool = False,
               regex: bool = False, limit: int | None = None,
               min_size: int | None = None, max_size: int | None = None,
               modified_after: float | None = None, modified_before: float | None = None,
               sort_by: str = "path", descending: bool = False,
               include_hidden: bool = False) -> list[dict]:
        """Search names or bounded text content with optional metadata filters.

        Modification timestamps are finite Unix seconds. Supported sort keys are path,
        name, size, and modified. Search is deterministic when sort keys tie.
        """
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if mode not in {"name", "content"}:
            raise ValueError("mode must be 'name' or 'content'")
        if not isinstance(case_sensitive, bool) or not isinstance(regex, bool):
            raise TypeError("case_sensitive and regex must be booleans")
        if not isinstance(descending, bool) or not isinstance(include_hidden, bool):
            raise TypeError("descending and include_hidden must be booleans")
        if limit is None:
            limit = self.max_results
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, self.max_results)
        root = self.engine.root if within == "." else self.engine.resolve(within)
        if root.is_symlink():
            raise UnsafePathError("search root symlinks are not supported")
        if not root.exists():
            return []
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        for field, value in (("min_size", min_size), ("max_size", max_size)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{field} must be a non-negative integer")
        if min_size is not None and max_size is not None and min_size > max_size:
            raise ValueError("min_size cannot exceed max_size")
        for field, value in (("modified_after", modified_after), ("modified_before", modified_before)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{field} must be a finite Unix timestamp")
                if not math.isfinite(value):
                    raise ValueError(f"{field} must be a finite Unix timestamp")
        if modified_after is not None and modified_before is not None and modified_after > modified_before:
            raise ValueError("modified_after cannot exceed modified_before")
        if sort_by not in {"path", "name", "size", "modified"}:
            raise ValueError("sort_by must be path, name, size, or modified")
        suffixes = None
        if extensions is not None:
            try:
                suffixes = set()
                for ext in extensions:
                    if not isinstance(ext, str) or not ext.strip():
                        raise ValueError("extensions must contain non-empty strings")
                    normalized = ext.strip().casefold()
                    suffixes.add(normalized if normalized.startswith(".") else "." + normalized)
            except TypeError as exc:
                raise TypeError("extensions must be an iterable of strings") from exc
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        results = []
        for path in self._files(root):
            if not include_hidden and any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            stat = path.stat()
            if min_size is not None and stat.st_size < min_size:
                continue
            if max_size is not None and stat.st_size > max_size:
                continue
            if modified_after is not None and stat.st_mtime < modified_after:
                continue
            if modified_before is not None and stat.st_mtime > modified_before:
                continue
            if suffixes is not None and path.suffix.casefold() not in suffixes:
                continue
            match = None
            if mode == "name":
                match = pattern.search(path.name)
            elif path.suffix.casefold() in self.TEXT_EXTENSIONS and stat.st_size <= self.max_file_bytes:
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                match = pattern.search(content)
            if match:
                results.append({"path": self._relative(path), "name": path.name,
                                "size": stat.st_size, "modified": stat.st_mtime,
                                "extension": path.suffix.casefold(), "mode": mode,
                                "match": match.group(0)[:160]})
        keys = {
            "path": lambda item: (item["path"].casefold(), item["path"]),
            "name": lambda item: (item["name"].casefold(), item["path"].casefold(), item["path"]),
            "size": lambda item: (item["size"], item["path"].casefold(), item["path"]),
            "modified": lambda item: (item["modified"], item["path"].casefold(), item["path"]),
        }
        results.sort(key=keys[sort_by], reverse=descending)
        return results[:limit]

    def search_folders(self, query: str, *, within: str = ".", case_sensitive: bool = False,
                       limit: int | None = None) -> list[dict]:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if not isinstance(case_sensitive, bool):
            raise TypeError("case_sensitive must be a boolean")
        root = self.engine.root if within == "." else self.engine.resolve(within)
        if not root.exists():
            return []
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        if root.is_symlink():
            raise UnsafePathError("search root symlinks are not supported")
        if limit is None:
            maximum = self.max_results
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        else:
            maximum = min(limit, self.max_results)
        needle = query if case_sensitive else query.casefold()
        found = []
        for current, dirs, _files in os.walk(root, topdown=True, followlinks=False):
            base = Path(current)
            dirs[:] = sorted([name for name in dirs if not (base / name).is_symlink()],
                             key=lambda value: (value.casefold(), value))
            for name in dirs:
                candidate = base / name
                if candidate.is_symlink():
                    continue
                candidate_name = name if case_sensitive else name.casefold()
                if needle in candidate_name:
                    found.append({"path": self._relative(candidate), "name": name})
                    if len(found) >= maximum:
                        return found
        return found

    def save_search(self, name: str, query: dict) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("saved search name must be non-empty")
        if not isinstance(query, dict):
            raise TypeError("query must be a dictionary")
        try:
            json.dumps(query)
        except (TypeError, ValueError) as exc:
            raise TypeError("saved search must be JSON serializable") from exc
        state = self._read_saved()
        state["searches"][name.strip()] = query
        self._write_saved(state)

    def saved_searches(self) -> dict:
        return copy.deepcopy(self._read_saved()["searches"])

    def run_saved_search(self, name: str) -> list[dict]:
        searches = self._read_saved()["searches"]
        if name not in searches:
            raise KeyError(name)
        saved = searches[name]
        if not isinstance(saved, dict) or "query" not in saved:
            raise FolderEngineError(f"saved search {name!r} has an invalid query format")
        query = dict(saved)
        term = query.pop("query")
        return self.search(term, **query)

    def delete_saved_search(self, name: str) -> bool:
        state = self._read_saved()
        existed = name in state["searches"]
        state["searches"].pop(name, None)
        if existed:
            self._write_saved(state)
        return existed

    def _read_saved(self) -> dict:
        if not self.state_path.exists():
            return {"version": 1, "searches": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FolderEngineError(f"saved searches are unreadable: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("searches"), dict):
            raise FolderEngineError("saved searches have an invalid format")
        return data

    def _write_saved(self, data: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.state_path.name + ".", suffix=".tmp",
                                         dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
