"""Bounded, symlink-safe workspace search with reusable saved queries."""
from __future__ import annotations

import json
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
               regex: bool = False, limit: int | None = None) -> list[dict]:
        """Search names or bounded text content. Regex errors are surfaced."""
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if mode not in {"name", "content"}:
            raise ValueError("mode must be 'name' or 'content'")
        if limit is None:
            limit = self.max_results
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, self.max_results)
        root = self.engine.root if within == "." else self.engine.resolve(within)
        if not root.exists():
            return []
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        suffixes = None
        if extensions is not None:
            suffixes = {ext.casefold() if ext.startswith(".") else "." + ext.casefold()
                        for ext in extensions}
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(query if regex else re.escape(query), flags)
        results = []
        for path in self._files(root):
            if suffixes is not None and path.suffix.casefold() not in suffixes:
                continue
            match = None
            if mode == "name":
                match = pattern.search(path.name)
            elif path.suffix.casefold() in self.TEXT_EXTENSIONS and path.stat().st_size <= self.max_file_bytes:
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                match = pattern.search(content)
            if match:
                results.append({"path": self._relative(path), "name": path.name,
                                "size": path.stat().st_size, "mode": mode,
                                "match": match.group(0)[:160]})
                if len(results) >= limit:
                    break
        return results

    def search_folders(self, query: str, *, within: str = ".", case_sensitive: bool = False,
                       limit: int | None = None) -> list[dict]:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        root = self.engine.root if within == "." else self.engine.resolve(within)
        if not root.exists():
            return []
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        needle = query if case_sensitive else query.casefold()
        maximum = min(limit or self.max_results, self.max_results)
        found = []
        for current, dirs, _files in os.walk(root, topdown=True, followlinks=False):
            base = Path(current)
            dirs[:] = sorted([name for name in dirs if not (base / name).is_symlink()],
                             key=lambda value: (value.casefold(), value))
            for name in dirs:
                candidate = base / name
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
        return {name: dict(query) for name, query in self._read_saved()["searches"].items()}

    def run_saved_search(self, name: str) -> list[dict]:
        searches = self._read_saved()["searches"]
        if name not in searches:
            raise KeyError(name)
        query = dict(searches[name])
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
