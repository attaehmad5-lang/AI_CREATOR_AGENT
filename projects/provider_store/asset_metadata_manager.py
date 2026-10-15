"""Durable, secret-safe provider attribution and asset metadata ledger."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class AssetMetadataManager:
    """Persist verified output metadata without storing absolute paths or credentials."""

    VERSION = 1
    _ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
    _HASH_PATTERN = re.compile(r"[a-f0-9]{64}\Z")
    _SECRET_MARKERS = ("token=", "api_key=", "password=", "secret=", "bearer ")

    def __init__(self, path: str | Path) -> None:
        if not isinstance(path, (str, Path)) or not str(path).strip() or "\x00" in str(path):
            raise ValueError("metadata path is invalid")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, interprocess_file_lock(self.lock_path):
            if not self.path.exists():
                self._write({"version": self.VERSION, "assets": {}})

    @classmethod
    def _safe_identifier(cls, value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(cls._ID_PATTERN.fullmatch(value))
            and not any(marker in value.casefold() for marker in cls._SECRET_MARKERS)
        )

    @classmethod
    def _validate_record(cls, record: dict[str, Any]) -> bool:
        if not isinstance(record, dict):
            return False
        if not all(cls._safe_identifier(record.get(field)) for field in ("provider_id", "request_id", "project_id")):
            return False
        if not cls._safe_identifier(record.get("modality")):
            return False
        if not isinstance(record.get("filename"), str) or not record["filename"] or record["filename"] in {".", ".."}:
            return False
        if Path(record["filename"]).name != record["filename"] or "/" in record["filename"] or "\\" in record["filename"]:
            return False
        if not isinstance(record.get("mime_type"), str) or not re.fullmatch(r"[A-Za-z0-9.+-]{1,80}/[A-Za-z0-9.+-]{1,80}", record["mime_type"]):
            return False
        size = record.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > 10**12:
            return False
        if not isinstance(record.get("sha256"), str) or not cls._HASH_PATTERN.fullmatch(record["sha256"]):
            return False
        timestamp = record.get("created_at")
        if not isinstance(timestamp, str):
            return False
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def _read(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("asset metadata store is unreadable") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != self.VERSION
            or not isinstance(loaded.get("assets"), dict)
        ):
            raise ValueError("asset metadata store schema is invalid")
        for asset_id, record in loaded["assets"].items():
            if not isinstance(asset_id, str) or not self._validate_record(record):
                raise ValueError("asset metadata store contains an invalid record")
            expected_id = self._asset_id(record)
            if asset_id != expected_id:
                raise ValueError("asset metadata store contains a mismatched asset ID")
        return loaded

    @staticmethod
    def _asset_id(record: dict[str, Any]) -> str:
        identity = "\0".join((
            record["project_id"], record["request_id"], record["provider_id"],
            record["filename"], record["sha256"],
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _write(self, data: dict[str, Any]) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def record_published_asset(
        self,
        *,
        provider_id: str,
        request_id: str,
        project_id: str,
        filename: str,
        modality: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "provider_id": provider_id,
            "request_id": request_id,
            "project_id": project_id,
            "filename": filename,
            "modality": modality.casefold() if isinstance(modality, str) else modality,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        if not self._validate_record(record):
            return {"saved": False, "status": "INVALID_ASSET_METADATA"}
        asset_id = self._asset_id(record)
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                loaded = self._read()
            except ValueError:
                return {"saved": False, "status": "ASSET_METADATA_STORE_INVALID"}
            existing = loaded["assets"].get(asset_id)
            if existing is not None:
                if existing == record:
                    return {"saved": True, "status": "ALREADY_RECORDED", "asset_id": asset_id, "record": copy.deepcopy(existing)}
                return {"saved": False, "status": "ASSET_METADATA_CONFLICT", "asset_id": asset_id}
            loaded["assets"][asset_id] = record
            try:
                self._write(loaded)
            except OSError:
                return {"saved": False, "status": "ASSET_METADATA_PERSIST_FAILED"}
        return {"saved": True, "status": "RECORDED", "asset_id": asset_id, "record": copy.deepcopy(record)}

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        if not isinstance(asset_id, str) or not self._HASH_PATTERN.fullmatch(asset_id):
            return {"status": "INVALID_ASSET_ID"}
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                loaded = self._read()
            except ValueError:
                return {"status": "ASSET_METADATA_STORE_INVALID"}
        record = loaded["assets"].get(asset_id)
        if record is None:
            return {"status": "NOT_FOUND", "asset_id": asset_id}
        return {"status": "FOUND", "asset_id": asset_id, "record": copy.deepcopy(record)}

    def list_assets(self) -> dict[str, Any]:
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                loaded = self._read()
            except ValueError:
                return {"status": "ASSET_METADATA_STORE_INVALID", "assets": []}
        return {
            "status": "OK",
            "assets": [
                {"asset_id": asset_id, "record": copy.deepcopy(record)}
                for asset_id, record in sorted(loaded["assets"].items())
            ],
        }
