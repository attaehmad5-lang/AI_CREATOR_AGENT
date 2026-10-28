"""Persistent, UI-agnostic navigation state for project folder trees.

This module deliberately separates explorer state from a GUI framework. Web,
desktop, and mobile interfaces can consume the same navigation and filtering
primitives without sharing mutable UI objects.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from projects.folder_engine import FolderEngine, FolderEngineError, FolderTreeNode, UnsafePathError


class FolderExplorer:
    """Provide safe navigation, recents, favorites, pins, and tree filtering."""

    VIEWS = {"tree", "grid", "list", "compact"}

    def __init__(self, engine: FolderEngine, *, state_path: str | os.PathLike[str] | None = None,
                 history_limit: int = 50) -> None:
        if not isinstance(engine, FolderEngine):
            raise TypeError("engine must be a FolderEngine")
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or history_limit < 1:
            raise ValueError("history_limit must be a positive integer")
        self.engine = engine
        self.history_limit = history_limit
        self.state_path = Path(state_path) if state_path is not None else engine.root / ".folder_explorer_state.json"
        self._history: list[str] = ["."]
        self._history_index = 0
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if self.state_path.is_symlink():
            raise UnsafePathError("explorer state file cannot be a symlink")
        if not self.state_path.exists():
            return {"version": 1, "recent": [], "favorites": [], "pinned": [],
                    "view": "tree", "sort": [{"field": "name", "descending": False}],
                    "filters": {}, "labels": {}, "icons": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FolderEngineError(f"explorer state is unreadable: {exc}") from exc
        version = data.get("version") if isinstance(data, dict) else None
        if (not isinstance(data, dict) or isinstance(version, bool)
                or not isinstance(version, int) or version != 1):
            raise FolderEngineError("unsupported explorer state format")
        for key in ("recent", "favorites", "pinned"):
            if not isinstance(data.get(key, []), list) or any(
                not isinstance(path, str) or not path or path.startswith("/")
                or "\\" in path or any(part in ("", "..") for part in path.split("/") if part != ".")
                for path in data.get(key, [])
            ):
                raise FolderEngineError(f"invalid explorer state field: {key}")
            data.setdefault(key, [])
        data.setdefault("view", "tree")
        data.setdefault("sort", [{"field": "name", "descending": False}])
        data.setdefault("filters", {})
        data.setdefault("labels", {})
        data.setdefault("icons", {})

        if not isinstance(data["view"], str) or data["view"] not in self.VIEWS:
            raise FolderEngineError("invalid explorer state field: view")
        sort_rules = data["sort"]
        if (not isinstance(sort_rules, list) or not sort_rules
                or any(not isinstance(rule, dict)
                       or rule.get("field") not in {"name", "path", "depth"}
                       or not isinstance(rule.get("descending"), bool)
                       for rule in sort_rules)):
            raise FolderEngineError("invalid explorer state field: sort")
        filters = data["filters"]
        if (not isinstance(filters, dict)
                or (filters.get("name_contains") is not None
                    and not isinstance(filters.get("name_contains"), str))
                or (filters.get("path_prefix") is not None
                    and (not isinstance(filters.get("path_prefix"), str)
                         or not filters.get("path_prefix")
                         or filters["path_prefix"].startswith("/")
                         or "\\" in filters["path_prefix"]
                         or any(part in ("", "..") for part in filters["path_prefix"].split("/")
                                if part != ".")))):
            raise FolderEngineError("invalid explorer state field: filters")
        for key in ("labels", "icons"):
            mapping = data[key]
            if (not isinstance(mapping, dict)
                    or any(not isinstance(path, str) or not path or path.startswith("/")
                           or "\\" in path
                           or any(part in ("", "..") for part in path.split("/") if part != ".")
                           or not isinstance(value, str) or not value.strip()
                           for path, value in mapping.items())):
                raise FolderEngineError(f"invalid explorer state field: {key}")
        return data

    def _save_state(self) -> None:
        if self.state_path.is_symlink():
            raise UnsafePathError("explorer state file cannot be a symlink")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.state_path.name + ".", suffix=".tmp",
                                         dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._state, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _path(self, path: str | os.PathLike[str], *, must_exist: bool = True) -> str:
        raw = os.fspath(path)
        if raw == ".":
            return "."
        target = self.engine.resolve(raw)
        relative = target.relative_to(self.engine.root).as_posix()
        if must_exist and not target.is_dir():
            raise FileNotFoundError(f"folder does not exist: {relative}")
        return relative

    def navigate(self, path: str | os.PathLike[str]) -> str:
        relative = self._path(path)
        if self._history[self._history_index] != relative:
            self._history = self._history[:self._history_index + 1]
            self._history.append(relative)
            self._history = self._history[-(self.history_limit + 1):]
            self._history_index = len(self._history) - 1
        if relative != ".":
            self._state["recent"] = [relative] + [p for p in self._state["recent"] if p != relative]
            self._state["recent"] = self._state["recent"][:self.history_limit]
            self._save_state()
        return relative

    @property
    def current_path(self) -> str:
        return self._history[self._history_index]

    def back(self) -> str:
        if self._history_index > 0:
            self._history_index -= 1
        return self.current_path

    def forward(self) -> str:
        if self._history_index + 1 < len(self._history):
            self._history_index += 1
        return self.current_path

    def breadcrumbs(self, path: str | os.PathLike[str] | None = None) -> list[dict[str, str]]:
        relative = self._path(self.current_path if path is None else path)
        if relative == ".":
            return [{"name": self.engine.root.name, "path": "."}]
        parts = relative.split("/")
        result = [{"name": self.engine.root.name, "path": "."}]
        prefix = []
        for part in parts:
            prefix.append(part)
            result.append({"name": part, "path": "/".join(prefix)})
        return result

    def recent(self) -> list[str]:
        return list(self._state["recent"])

    def favorites(self) -> list[str]:
        return list(self._state["favorites"])

    def pinned(self) -> list[str]:
        return list(self._state["pinned"])

    def _set_membership(self, key: str, path: str | os.PathLike[str], enabled: bool) -> None:
        relative = self._path(path)
        if relative == ".":
            raise UnsafePathError("workspace root cannot be favorited or pinned")
        values = [p for p in self._state[key] if p != relative]
        if enabled:
            values.insert(0, relative)
        self._state[key] = values[:self.history_limit]
        self._save_state()

    def set_favorite(self, path: str | os.PathLike[str], enabled: bool = True) -> None:
        self._set_membership("favorites", path, enabled)

    def set_pinned(self, path: str | os.PathLike[str], enabled: bool = True) -> None:
        self._set_membership("pinned", path, enabled)

    def set_view(self, view: str) -> None:
        if view not in self.VIEWS:
            raise ValueError(f"view must be one of {sorted(self.VIEWS)}")
        self._state["view"] = view
        self._save_state()

    @property
    def view(self) -> str:
        return self._state["view"]

    def set_sort(self, fields: Iterable[tuple[str, bool]]) -> None:
        normalized = []
        for field, descending in fields:
            if field not in {"name", "path", "depth"}:
                raise ValueError(f"unsupported sort field: {field}")
            if not isinstance(descending, bool):
                raise TypeError("descending must be a bool")
            normalized.append({"field": field, "descending": descending})
        if not normalized:
            raise ValueError("at least one sort field is required")
        self._state["sort"] = normalized
        self._save_state()

    def set_filter(self, *, name_contains: str | None = None, path_prefix: str | None = None) -> None:
        if name_contains is not None and not isinstance(name_contains, str):
            raise TypeError("name_contains must be a string or None")
        if path_prefix is not None:
            path_prefix = self._path(path_prefix, must_exist=False)
        self._state["filters"] = {"name_contains": name_contains, "path_prefix": path_prefix}
        self._save_state()

    def set_label(self, path: str | os.PathLike[str], label: str | None) -> None:
        relative = self._path(path)
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ValueError("label must be a non-empty string or None")
        if label is None:
            self._state["labels"].pop(relative, None)
        else:
            self._state["labels"][relative] = label.strip()
        self._save_state()

    def set_icon(self, path: str | os.PathLike[str], icon: str | None) -> None:
        relative = self._path(path)
        if icon is not None and (not isinstance(icon, str) or not icon.strip()):
            raise ValueError("icon must be a non-empty string or None")
        if icon is None:
            self._state["icons"].pop(relative, None)
        else:
            self._state["icons"][relative] = icon.strip()
        self._save_state()

    def overview(self, path: str | os.PathLike[str] = ".") -> dict:
        relative = self._path(path)
        target = self.engine.root if relative == "." else self.engine.resolve(relative)
        children = self.engine.list_folders(relative)
        files = sum(1 for item in target.iterdir() if item.is_file() and not item.is_symlink())
        return {"path": relative, "folder_count": len(children), "file_count": files,
                "child_folders": [item.relative_path for item in children],
                "favorite": relative in self._state["favorites"],
                "pinned": relative in self._state["pinned"],
                "label": self._state["labels"].get(relative),
                "icon": self._state["icons"].get(relative)}

    def tree(self, path: str | os.PathLike[str] = ".", *, max_depth: int | None = None) -> tuple[FolderTreeNode, ...]:
        relative = self._path(path)
        roots = self.engine.list_tree(relative, max_depth=max_depth)
        filters = self._state["filters"]
        name_query = (filters.get("name_contains") or "").casefold()
        prefix = filters.get("path_prefix")
        sort_rules = self._state["sort"]

        def sort_children(nodes: tuple[FolderTreeNode, ...]) -> list[FolderTreeNode]:
            ordered = list(nodes)
            # Stable multi-field sort: the last rule is applied first.
            for rule in reversed(sort_rules):
                field = rule["field"]
                def key(node: FolderTreeNode):
                    if field == "name":
                        return node.entry.name.casefold()
                    if field == "path":
                        return node.entry.relative_path.casefold()
                    return node.entry.relative_path.count("/") + (node.entry.relative_path != ".")
                ordered.sort(key=key, reverse=rule["descending"])
            return ordered

        def filter_node(node: FolderTreeNode) -> FolderTreeNode | None:
            children = tuple(filtered for child in sort_children(node.children)
                             if (filtered := filter_node(child)) is not None)
            node_path = node.entry.relative_path
            matches_name = not name_query or name_query in node.entry.name.casefold()
            matches_prefix = not prefix or node_path == prefix or node_path.startswith(prefix.rstrip("/") + "/")
            if matches_name and matches_prefix:
                return FolderTreeNode(node.entry, children)
            if children:
                return FolderTreeNode(node.entry, children)
            return None

        return tuple(filtered for root in roots if (filtered := filter_node(root)) is not None)
