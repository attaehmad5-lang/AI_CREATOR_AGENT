"""Validated JSON catalog import/export with atomic replacement and rollback."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connection_manager import ConnectionManager
from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


def _read_valid_catalog(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            catalog = json.load(stream)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("catalog file is unreadable or invalid JSON") from exc
    result = ConnectionManager.validate_catalog_schema(catalog)
    if result.get("valid") is not True:
        raise ValueError("catalog schema validation failed")
    return catalog


def export_catalog(
    source: str | os.PathLike[str], destination: str | os.PathLike[str]
) -> dict[str, Any]:
    """Export only a schema-valid catalog; never copy malformed JSON or credential fields."""
    source_path = Path(source)
    destination_path = Path(destination)
    catalog = _read_valid_catalog(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(catalog, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace_with_retry(temp_name, destination_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return {
        "status": "CATALOG_EXPORTED",
        "exported": True,
        "provider_count": len(catalog["providers"]),
    }


def import_catalog(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    backup: bool = True,
) -> dict[str, Any]:
    """Validate before locking, then reload destination under lock and atomically replace it.

    Existing destination content is preserved in a .bak file by default. If a post-write
    validation fails, the original bytes are restored atomically before returning.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if not isinstance(backup, bool):
        raise ValueError("backup must be a boolean")
    candidate = _read_valid_catalog(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination_path.with_name(destination_path.name + ".lock")
    backup_path = destination_path.with_name(destination_path.name + ".bak")
    with interprocess_file_lock(lock_path):
        original_bytes: bytes | None = (
            destination_path.read_bytes() if destination_path.exists() else None
        )
        if backup and original_bytes is not None:
            fd_backup, temp_backup = tempfile.mkstemp(
                prefix=f".{backup_path.name}.",
                suffix=".tmp",
                dir=destination_path.parent,
            )
            try:
                with os.fdopen(fd_backup, "wb") as stream:
                    stream.write(original_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                atomic_replace_with_retry(temp_backup, backup_path)
            except Exception:
                try:
                    os.close(fd_backup)
                except OSError:
                    pass
                try:
                    os.unlink(temp_backup)
                except FileNotFoundError:
                    pass
                return {"status": "CATALOG_BACKUP_FAILED", "imported": False}
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    candidate, stream, ensure_ascii=False, indent=2, allow_nan=False
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, destination_path)
            verified = _read_valid_catalog(destination_path)
            if verified != candidate:
                raise ValueError("catalog verification mismatch")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            try:
                if original_bytes is None:
                    destination_path.unlink(missing_ok=True)
                else:
                    fd_restore, restore_name = tempfile.mkstemp(
                        prefix=f".{destination_path.name}.restore.",
                        suffix=".tmp",
                        dir=destination_path.parent,
                    )
                    with os.fdopen(fd_restore, "wb") as stream:
                        stream.write(original_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                    atomic_replace_with_retry(restore_name, destination_path)
            except Exception:
                return {"status": "CATALOG_IMPORT_ROLLBACK_FAILED", "imported": False}
            return {"status": "CATALOG_IMPORT_FAILED_ROLLED_BACK", "imported": False}
    return {
        "status": "CATALOG_IMPORTED",
        "imported": True,
        "provider_count": len(candidate["providers"]),
    }


def create_catalog_snapshot(
    source: str | os.PathLike[str],
    history_directory: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create an immutable, validated JSON snapshot in a history directory."""
    catalog = _read_valid_catalog(Path(source))
    history_path = Path(history_directory)
    history_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for sequence in range(1000):
        suffix = f"-{sequence:03d}" if sequence else ""
        snapshot = history_path / f"catalog-{timestamp}{suffix}.json"
        try:
            with snapshot.open("x", encoding="utf-8") as stream:
                json.dump(
                    catalog, stream, ensure_ascii=False, indent=2, allow_nan=False
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            continue
        except Exception:
            snapshot.unlink(missing_ok=True)
            raise
        return {
            "status": "CATALOG_SNAPSHOT_CREATED",
            "created": True,
            "snapshot": snapshot.name,
            "provider_count": len(catalog["providers"]),
        }
    raise FileExistsError("could not allocate a unique catalog snapshot filename")


def list_catalog_snapshots(
    history_directory: str | os.PathLike[str],
) -> dict[str, Any]:
    """List valid snapshots without returning provider configuration or credentials."""
    history_path = Path(history_directory)
    if not history_path.exists():
        return {"status": "CATALOG_HISTORY_EMPTY", "snapshots": []}
    snapshots = []
    for path in sorted(history_path.glob("catalog-*.json"), reverse=True):
        try:
            catalog = _read_valid_catalog(path)
        except (OSError, ValueError):
            continue
        snapshots.append(
            {"name": path.name, "provider_count": len(catalog["providers"])}
        )
    return {
        "status": "CATALOG_HISTORY_LISTED" if snapshots else "CATALOG_HISTORY_EMPTY",
        "snapshots": snapshots,
    }


def restore_catalog_snapshot(
    snapshot: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> dict[str, Any]:
    """Restore a validated snapshot through the atomic import/backup path."""
    snapshot_path = Path(snapshot)
    if (
        not snapshot_path.is_file()
        or not snapshot_path.name.startswith("catalog-")
        or snapshot_path.suffix != ".json"
    ):
        raise ValueError("snapshot must be an existing catalog-*.json file")
    _read_valid_catalog(snapshot_path)
    result = import_catalog(snapshot_path, destination, backup=True)
    if not result.get("imported"):
        return {"status": "CATALOG_SNAPSHOT_RESTORE_FAILED", "restored": False}
    return {
        "status": "CATALOG_SNAPSHOT_RESTORED",
        "restored": True,
        "provider_count": result["provider_count"],
    }
