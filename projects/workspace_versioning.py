"""Content-addressed file version history and restore for a workspace.

This local implementation is intentionally single-process. Version blobs and
manifest live under a reserved hidden directory inside the workspace root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from projects.folder_engine import FolderEngine, UnsafePathError


class VersioningError(ValueError):
    """Raised when a version cannot safely be created or restored."""


class WorkspaceVersionStore:
    STORE_NAME = ".workspace_versions"
    MANIFEST_NAME = "manifest.json"

    def __init__(self, engine: FolderEngine, *, max_versions_per_file: int = 20,
                 max_file_bytes: int = 100 * 1024 * 1024) -> None:
        if not isinstance(engine, FolderEngine):
            raise TypeError("engine must be a FolderEngine")
        for name, value in (("max_versions_per_file", max_versions_per_file),
                            ("max_file_bytes", max_file_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.engine = engine
        self.max_versions_per_file = max_versions_per_file
        self.max_file_bytes = max_file_bytes
        self._lock = threading.RLock()
        self.store = self.engine.root / self.STORE_NAME
        self.blobs = self.store / "blobs"
        self.manifest_path = self.store / self.MANIFEST_NAME
        self._ensure_store()

    def _ensure_store(self) -> None:
        self.engine.ensure_root()
        # Never follow links for version data: they could redirect writes
        # outside the workspace or expose another directory's contents.
        if self.store.is_symlink():
            raise UnsafePathError("version store cannot be a symlink")
        self.store.mkdir(parents=True, exist_ok=True)
        if self.store.resolve() != self.engine.root.resolve() / self.STORE_NAME:
            raise UnsafePathError("version store must remain inside the workspace")
        if self.blobs.is_symlink():
            raise UnsafePathError("version blob directory cannot be a symlink")
        self.blobs.mkdir(parents=True, exist_ok=True)
        if self.blobs.resolve().parent != self.store.resolve():
            raise UnsafePathError("version blobs must remain inside the version store")
        if self.manifest_path.is_symlink():
            raise UnsafePathError("version manifest cannot be a symlink")
        if not self.manifest_path.exists():
            self._write_manifest({"schema_version": 1, "versions": []})

    def _read_manifest(self) -> dict[str, Any]:
        if self.store.is_symlink() or self.blobs.is_symlink() or self.manifest_path.is_symlink():
            raise UnsafePathError("version storage paths cannot be symlinks")
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VersioningError("version manifest is unreadable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("versions"), list):
            raise VersioningError("version manifest schema is invalid")
        seen_ids: set[str] = set()
        for item in value["versions"]:
            if not isinstance(item, dict) or not all(
                k in item for k in ("id", "path", "blob", "sha256", "size", "created_at")
            ):
                raise VersioningError("version manifest entry is invalid")
            version_id = item["id"]
            path = item["path"]
            blob = item["blob"]
            digest = item["sha256"]
            size = item["size"]
            created_at = item["created_at"]
            if not isinstance(version_id, str) or not version_id or version_id in seen_ids:
                raise VersioningError("version manifest contains an invalid or duplicate ID")
            seen_ids.add(version_id)
            if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
                raise VersioningError("version manifest contains an invalid path")
            parts = path.split("/")
            if any(part in ("", ".", "..") for part in parts) or parts[0] == self.STORE_NAME:
                raise VersioningError("version manifest contains an unsafe path")
            if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise VersioningError("version manifest contains an invalid digest")
            if not isinstance(blob, str) or blob != digest + ".blob":
                raise VersioningError("version manifest contains an invalid blob reference")
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.max_file_bytes:
                raise VersioningError("version manifest contains an invalid size")
            if isinstance(created_at, bool) or not isinstance(created_at, (int, float)) or not __import__("math").isfinite(created_at):
                raise VersioningError("version manifest contains an invalid timestamp")
            if "note" in item and not isinstance(item["note"], str):
                raise VersioningError("version manifest contains an invalid note")
        return value

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        if self.store.is_symlink() or self.blobs.is_symlink() or self.manifest_path.is_symlink():
            raise UnsafePathError("version storage paths cannot be symlinks")
        self.store.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=str(self.store))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.manifest_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _target(self, relative_path: str) -> Path:
        target = self.engine.resolve(relative_path)
        if target == self.engine.root or target.is_relative_to(self.store):
            raise UnsafePathError("reserved version-store paths cannot be versioned")
        if target.is_symlink():
            raise UnsafePathError("symlink versioning is not supported")
        return target

    @staticmethod
    def _hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def create_version(self, relative_path: str, *, note: str = "") -> dict[str, Any]:
        with self._lock:
            return self._create_version_locked(relative_path, note=note)

    def _create_version_locked(self, relative_path: str, *, note: str) -> dict[str, Any]:
        if not isinstance(note, str):
            raise VersioningError("note must be a string")
        target = self._target(relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        if target.stat().st_size > self.max_file_bytes:
            raise VersioningError("file exceeds versioning size limit")
        data = target.read_bytes()
        digest = self._hash(data)
        blob_name = f"{digest}.blob"
        blob = self.blobs / blob_name
        if not blob.exists():
            fd, temp_name = tempfile.mkstemp(prefix=".blob-", suffix=".tmp", dir=str(self.blobs))
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temp_name, blob)
                except FileExistsError:
                    pass
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        manifest = self._read_manifest()
        entry = {"id": uuid.uuid4().hex, "path": target.relative_to(self.engine.root).as_posix(),
                 "blob": blob_name, "sha256": digest, "size": len(data),
                 "created_at": time.time(), "note": str(note)[:500]}
        manifest["versions"].append(entry)
        related = [v for v in manifest["versions"] if v["path"] == entry["path"]]
        if len(related) > self.max_versions_per_file:
            remove_ids = {v["id"] for v in related[:-self.max_versions_per_file]}
            manifest["versions"] = [v for v in manifest["versions"] if v["id"] not in remove_ids]
        self._write_manifest(manifest)
        self._prune_unreferenced_blobs(manifest)
        return dict(entry)

    def list_versions(self, relative_path: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._list_versions_locked(relative_path)

    def _list_versions_locked(self, relative_path: str) -> list[dict[str, Any]]:
        target = self._target(relative_path)
        path = target.relative_to(self.engine.root).as_posix()
        entries = [dict(v) for v in self._read_manifest()["versions"] if v["path"] == path]
        return sorted(entries, key=lambda v: (v["created_at"], v["id"]), reverse=True)

    def restore_version(self, version_id: str) -> dict[str, Any]:
        with self._lock:
            return self._restore_version_locked(version_id)

    def _restore_version_locked(self, version_id: str) -> dict[str, Any]:
        if not isinstance(version_id, str) or not version_id:
            raise VersioningError("version_id must be non-empty")
        manifest = self._read_manifest()
        entry = next((v for v in manifest["versions"] if v["id"] == version_id), None)
        if entry is None:
            raise KeyError(version_id)
        blob_name = entry["blob"]
        if not isinstance(blob_name, str) or Path(blob_name).name != blob_name or not blob_name.endswith(".blob"):
            raise VersioningError("invalid blob reference")
        blob = self.blobs / blob_name
        if blob.is_symlink() or not blob.is_file():
            raise VersioningError("version blob is missing or unsafe")
        data = blob.read_bytes()
        if len(data) != entry["size"] or self._hash(data) != entry["sha256"]:
            raise VersioningError("version blob integrity check failed")
        target = self._target(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".restore-", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if target.is_symlink():
                raise UnsafePathError("cannot restore over a symlink")
            os.replace(temp_name, target)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return {"path": entry["path"], "size": len(data), "sha256": entry["sha256"], "version_id": version_id}

    def delete_version(self, version_id: str) -> bool:
        with self._lock:
            return self._delete_version_locked(version_id)

    def _delete_version_locked(self, version_id: str) -> bool:
        manifest = self._read_manifest()
        updated = [v for v in manifest["versions"] if v["id"] != version_id]
        if len(updated) == len(manifest["versions"]):
            return False
        manifest["versions"] = updated
        self._write_manifest(manifest)
        self._prune_unreferenced_blobs(manifest)
        return True

    def _prune_unreferenced_blobs(self, manifest: dict[str, Any]) -> None:
        used = {v["blob"] for v in manifest["versions"]}
        for blob in self.blobs.glob("*.blob"):
            if blob.name not in used and not blob.is_symlink():
                try:
                    blob.unlink()
                except OSError:
                    # Orphan cleanup is best-effort; it must not invalidate the
                    # already-committed manifest operation.
                    pass
