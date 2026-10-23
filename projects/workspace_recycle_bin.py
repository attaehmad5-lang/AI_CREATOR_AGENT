"""Workspace-contained soft deletion, restore, expiry and audit primitives."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from projects.folder_engine import FolderEngine, FolderEngineError, UnsafePathError


class RecycleBinError(FolderEngineError):
    """Base error for recycle-bin operations."""


class WorkspaceRecycleBin:
    """Move deleted workspace items into a private trash area with a manifest.

    This is a filesystem primitive, not an access-control boundary. Callers must
    enforce user/project authorization before invoking it.
    """

    def __init__(self, engine: FolderEngine, *, retention_seconds: int = 30 * 86400,
                 state_path: str | os.PathLike[str] | None = None) -> None:
        if not isinstance(engine, FolderEngine):
            raise TypeError("engine must be a FolderEngine")
        if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int) or retention_seconds < 0:
            raise ValueError("retention_seconds must be a non-negative integer")
        self.engine = engine
        self.retention_seconds = retention_seconds
        self.trash = engine.root / ".recycle_bin"
        self.state_path = Path(state_path) if state_path is not None else self.trash / "manifest.json"

    def _resolve(self, relative: str | os.PathLike[str]) -> Path:
        target = self.engine.resolve(relative)
        if target == self.engine.root or target == self.trash or self.trash in target.parents:
            raise UnsafePathError("workspace root and recycle-bin internals cannot be operated on")
        return target

    def _read(self) -> dict:
        if self.trash.is_symlink():
            raise UnsafePathError("recycle-bin directory cannot be a symlink")
        if self.state_path.is_symlink():
            raise UnsafePathError("recycle-bin manifest cannot be a symlink")
        if not self.state_path.exists():
            return {"version": 1, "items": [], "audit": []}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecycleBinError(f"recycle-bin manifest is unreadable: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("items"), list) or not isinstance(state.get("audit"), list):
            raise RecycleBinError("recycle-bin manifest has invalid format")
        seen_ids: set[str] = set()
        for item in state["items"]:
            if not isinstance(item, dict):
                raise RecycleBinError("recycle-bin item entry is invalid")
            item_id = item.get("id")
            original = item.get("original_path")
            payload_name = item.get("payload")
            deleted_at = item.get("deleted_at")
            expires_at = item.get("expires_at")
            if (not isinstance(item_id, str) or len(item_id) != 32
                    or any(char not in "0123456789abcdef" for char in item_id)
                    or item_id in seen_ids
                    or not isinstance(original, str) or not original
                    or original.startswith("/") or "\\" in original
                    or any(part in ("", ".", "..") for part in original.split("/"))
                    or not isinstance(payload_name, str) or payload_name != item_id
                    or not isinstance(deleted_at, (int, float)) or isinstance(deleted_at, bool)
                    or not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool)):
                raise RecycleBinError("recycle-bin item entry is unsafe")
            seen_ids.add(item_id)
        for event in state["audit"]:
            if not isinstance(event, dict) or not isinstance(event.get("action"), str):
                raise RecycleBinError("recycle-bin audit entry is invalid")
        return state

    def _write(self, state: dict) -> None:
        if self.trash.is_symlink() or self.state_path.is_symlink():
            raise UnsafePathError("recycle-bin storage paths cannot be symlinks")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="manifest-", suffix=".tmp", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.state_path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _audit(self, state: dict, action: str, item_id: str, path: str, timestamp: float) -> None:
        state["audit"].append({"timestamp": timestamp, "action": action, "item_id": item_id, "path": path})
        # Bound metadata growth; production deployments should ship audit events
        # to an append-only external audit service.
        state["audit"] = state["audit"][-10000:]

    def delete(self, relative: str | os.PathLike[str]) -> dict:
        source = self._resolve(relative)
        if source.is_symlink() or not source.exists():
            raise FileNotFoundError(str(source))
        if not (source.is_file() or source.is_dir()):
            raise RecycleBinError("unsupported filesystem object")
        state = self._read()
        self.trash.mkdir(parents=True, exist_ok=True)
        item_id = uuid.uuid4().hex
        payload = self.trash / item_id
        original = source.relative_to(self.engine.root).as_posix()
        now = time.time()
        # Rename is atomic on a single filesystem and does not follow a symlink.
        source.rename(payload)
        item = {"id": item_id, "original_path": original, "payload": item_id,
                "deleted_at": now, "expires_at": now + self.retention_seconds,
                "is_directory": payload.is_dir()}
        state["items"].append(item)
        self._audit(state, "soft_delete", item_id, original, now)
        try:
            self._write(state)
        except Exception:
            # Best-effort compensation: restore the item if manifest persistence fails.
            destination = self.engine.root / original
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                payload.rename(destination)
            raise
        return dict(item)

    def items(self) -> list[dict]:
        return [dict(item) for item in self._read()["items"]]

    def audit_log(self) -> list[dict]:
        return [dict(event) for event in self._read()["audit"]]

    def restore(self, item_id: str, *, destination: str | os.PathLike[str] | None = None) -> str:
        state = self._read()
        item = next((entry for entry in state["items"] if entry.get("id") == item_id), None)
        if item is None:
            raise KeyError(item_id)
        payload = self.trash / item["payload"]
        if payload.is_symlink() or not payload.exists():
            raise RecycleBinError("recycle-bin payload is missing or unsafe")
        raw_destination = destination if destination is not None else item["original_path"]
        target = self._resolve(raw_destination)
        if target.exists():
            raise FileExistsError(str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload.rename(target)
        state["items"].remove(item)
        now = time.time()
        self._audit(state, "restore", item_id, target.relative_to(self.engine.root).as_posix(), now)
        try:
            self._write(state)
        except Exception:
            # Best-effort compensation preserves the prior manifest/payload pair.
            if target.exists() and not payload.exists():
                target.rename(payload)
            state["items"].append(item)
            state["items"].sort(key=lambda entry: entry.get("deleted_at", 0))
            raise
        return target.relative_to(self.engine.root).as_posix()

    def purge_expired(self, *, now: float | None = None) -> list[str]:
        cutoff = time.time() if now is None else now
        if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
            raise TypeError("now must be a timestamp")
        state = self._read()
        purged = []
        retained = []
        for item in state["items"]:
            if item.get("expires_at", float("inf")) > cutoff:
                retained.append(item)
                continue
            payload = self.trash / item["payload"]
            if payload.is_symlink():
                payload.unlink()
            elif payload.is_dir():
                shutil.rmtree(payload)
            elif payload.exists():
                payload.unlink()
            purged.append(item["id"])
            self._audit(state, "permanent_delete", item["id"], item["original_path"], float(cutoff))
        state["items"] = retained
        if purged:
            self._write(state)
        return purged
