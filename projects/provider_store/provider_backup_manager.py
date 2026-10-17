"""Verified, bounded JSON-state backup snapshots with explicit restore controls.

Backups exclude obvious secret-bearing paths and secret-keyed JSON records. Restoring several
files is not a distributed transaction; this helper stages files first and attempts rollback
if a replacement fails. Production DR still requires external copies and timed restore drills.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class ProviderBackupManager:
    _NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
    _SECRET_MARKERS = ("token", "secret", "password", "api_key", "authorization", "credential", "private_key")

    def __init__(
        self,
        root: str | os.PathLike[str],
        backup_dir: str | os.PathLike[str],
        *,
        max_file_bytes: int = 50_000_000,
        max_total_bytes: int = 200_000_000,
    ) -> None:
        if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or not 1 <= max_file_bytes <= 500_000_000:
            raise ValueError("max_file_bytes is outside safe limits")
        if not isinstance(max_total_bytes, int) or isinstance(max_total_bytes, bool) or not max_file_bytes <= max_total_bytes <= 1_000_000_000:
            raise ValueError("max_total_bytes is outside safe limits")
        self.root = Path(root).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    @staticmethod
    def _safe_relative(value: Any) -> str | None:
        if not isinstance(value, str) or not value or "\\" in value:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or path.suffix.casefold() != ".json":
            return None
        return path.as_posix()

    @classmethod
    def _has_secret_key(cls, value: Any) -> bool:
        if isinstance(value, dict):
            for key, nested in value.items():
                if isinstance(key, str) and any(marker in key.casefold() for marker in cls._SECRET_MARKERS):
                    return True
                if cls._has_secret_key(nested):
                    return True
        elif isinstance(value, list):
            return any(cls._has_secret_key(item) for item in value)
        return False

    @classmethod
    def _safe_content(cls, raw: bytes) -> bool:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return False
        return not cls._has_secret_key(parsed)

    def _source_path(self, relative: str) -> Path | None:
        safe = self._safe_relative(relative)
        if safe is None:
            return None
        path = (self.root / safe).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        current = self.root
        for part in PurePosixPath(safe).parts:
            current = current / part
            if current.is_symlink():
                return None
        if any(marker in part.casefold() for part in PurePosixPath(safe).parts for marker in self._SECRET_MARKERS):
            return None
        return path

    def _backup_path(self, name: str) -> Path | None:
        if not isinstance(name, str) or not self._NAME.fullmatch(name):
            return None
        return self.backup_dir / f"{name}.zip"

    def create_backup(self, *, name: str, files: list[str]) -> dict[str, Any]:
        destination = self._backup_path(name)
        if destination is None:
            return {"status": "INVALID_BACKUP_NAME", "created": False}
        if not isinstance(files, list) or not files or len(files) > 1000:
            return {"status": "INVALID_BACKUP_FILE_LIST", "created": False}
        normalized: list[tuple[str, Path, bytes]] = []
        seen: set[str] = set()
        total = 0
        for relative in files:
            safe = self._safe_relative(relative)
            path = self._source_path(relative) if safe is not None else None
            if safe is None or path is None or safe in seen:
                return {"status": "UNSAFE_BACKUP_PATH", "created": False}
            seen.add(safe)
            try:
                if not path.is_file():
                    return {"status": "BACKUP_SOURCE_NOT_FILE", "created": False}
                if path.stat().st_size > self.max_file_bytes:
                    return {"status": "BACKUP_FILE_SIZE_EXCEEDED", "created": False}
                raw = path.read_bytes()
            except OSError:
                return {"status": "BACKUP_SOURCE_UNREADABLE", "created": False}
            if len(raw) > self.max_file_bytes or not self._safe_content(raw):
                return {"status": "BACKUP_SOURCE_INVALID_OR_SENSITIVE", "created": False}
            total += len(raw)
            if total > self.max_total_bytes:
                return {"status": "BACKUP_TOTAL_SIZE_EXCEEDED", "created": False}
            normalized.append((safe, path, raw))
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {
                name: {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                for name, _, raw in sorted(normalized)
            },
        }
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return {"status": "BACKUP_ALREADY_EXISTS", "created": False}
        fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(self.backup_dir))
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
                for relative, _, raw in normalized:
                    archive.writestr(f"files/{relative}", raw)
            os.replace(temp_name, destination)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            return {"status": "BACKUP_WRITE_FAILED", "created": False}
        return {"status": "BACKUP_CREATED", "created": True, "file_count": len(normalized), "total_bytes": total}

    def verify_backup(self, *, name: str) -> dict[str, Any]:
        archive_path = self._backup_path(name)
        if archive_path is None:
            return {"status": "INVALID_BACKUP_NAME", "verified": False}
        if archive_path.is_symlink() or not archive_path.is_file():
            return {"status": "BACKUP_NOT_FOUND", "verified": False}
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                infos = archive.infolist()
                if len(infos) > 1001 or len({item.filename for item in infos}) != len(infos):
                    return {"status": "BACKUP_ARCHIVE_STRUCTURE_INVALID", "verified": False}
                if any(item.file_size > self.max_file_bytes for item in infos):
                    return {"status": "BACKUP_FILE_SIZE_EXCEEDED", "verified": False}
                if sum(item.file_size for item in infos) > self.max_total_bytes + 1_000_000:
                    return {"status": "BACKUP_TOTAL_SIZE_EXCEEDED", "verified": False}
                if "manifest.json" not in {item.filename for item in infos}:
                    return {"status": "BACKUP_MANIFEST_MISSING", "verified": False}
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
                    return {"status": "BACKUP_MANIFEST_INVALID", "verified": False}
                expected_names = set(manifest["files"])
                actual_names = {item.filename[6:] for item in infos if item.filename.startswith("files/")}
                if expected_names != actual_names or len(expected_names) != len(infos) - 1:
                    return {"status": "BACKUP_FILE_LIST_MISMATCH", "verified": False}
                total = 0
                for relative, metadata in manifest["files"].items():
                    if self._safe_relative(relative) != relative or not isinstance(metadata, dict):
                        return {"status": "BACKUP_PATH_INVALID", "verified": False}
                    raw = archive.read(f"files/{relative}")
                    total += len(raw)
                    if (
                        len(raw) != metadata.get("size_bytes")
                        or hashlib.sha256(raw).hexdigest() != metadata.get("sha256")
                        or not self._safe_content(raw)
                    ):
                        return {"status": "BACKUP_CHECKSUM_OR_CONTENT_INVALID", "verified": False}
                if total > self.max_total_bytes:
                    return {"status": "BACKUP_TOTAL_SIZE_EXCEEDED", "verified": False}
        except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
            return {"status": "BACKUP_ARCHIVE_INVALID", "verified": False}
        return {"status": "BACKUP_VERIFIED", "verified": True, "file_count": len(expected_names), "total_bytes": total}

    def restore_backup(self, *, name: str, overwrite: bool = False) -> dict[str, Any]:
        if not isinstance(overwrite, bool):
            return {"status": "INVALID_OVERWRITE_FLAG", "restored": False}
        verification = self.verify_backup(name=name)
        if verification.get("verified") is not True:
            return {"status": "BACKUP_NOT_VERIFIED", "restored": False, "verification": verification}
        archive_path = self._backup_path(name)
        staged: list[tuple[Path, Path, bytes, bytes | None]] = []
        total = 0
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                for relative in sorted(manifest["files"]):
                    target = self._source_path(relative)
                    if target is None:
                        return {"status": "UNSAFE_RESTORE_PATH", "restored": False}
                    if target.exists() and not overwrite:
                        return {"status": "RESTORE_TARGET_EXISTS", "restored": False, "path": relative}
                    raw = archive.read(f"files/{relative}")
                    if target.exists() and target.stat().st_size > self.max_file_bytes:
                        return {"status": "RESTORE_TARGET_SIZE_EXCEEDED", "restored": False, "path": relative}
                    original = target.read_bytes() if target.exists() else None
                    total += len(raw) + (len(original) if original else 0)
                    if total > self.max_total_bytes * 2:
                        return {"status": "RESTORE_TOTAL_SIZE_EXCEEDED", "restored": False}
                    staged.append((target, target.parent / f".{target.name}.restore.tmp", raw, original))
        except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "BACKUP_ARCHIVE_INVALID", "restored": False}

        written: list[tuple[Path, bytes | None]] = []
        try:
            for target, temporary, raw, original in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            for target, temporary, raw, original in staged:
                os.replace(temporary, target)
                written.append((target, original))
        except OSError:
            rollback_ok = True
            for target, original in reversed(written):
                try:
                    if original is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.write_bytes(original)
                except OSError:
                    rollback_ok = False
            for _, temporary, _, _ in staged:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return {"status": "RESTORE_FAILED" if rollback_ok else "RESTORE_ROLLBACK_FAILED", "restored": False}
        return {"status": "RESTORED", "restored": True, "file_count": len(staged)}
