"""Validated, atomic publication of provider-generated output files."""
from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any


def publish_validated_output(
    temporary_path: str | Path,
    destination_path: str | Path,
    *,
    modality: str,
    validator: Any,
) -> dict[str, Any]:
    """Validate a temporary artifact before atomically replacing the destination.

    The temporary artifact must be in the destination directory so os.replace()
    remains atomic on the same filesystem. The previous destination is preserved
    unless validation, a stable content hash, and the atomic replace all succeed.
    Temporary artifacts are cleaned on handled failure paths.
    """
    if (
        not isinstance(temporary_path, (str, Path))
        or not str(temporary_path).strip()
        or "\x00" in str(temporary_path)
        or not isinstance(destination_path, (str, Path))
        or not str(destination_path).strip()
        or "\x00" in str(destination_path)
    ):
        return {"published": False, "status": "INVALID_PATH", "cleanup_succeeded": False}
    if not callable(getattr(validator, "validate_file", None)):
        return {"published": False, "status": "VALIDATOR_UNAVAILABLE", "cleanup_succeeded": False}

    temporary = Path(temporary_path)
    destination = Path(destination_path)
    try:
        if os.path.abspath(temporary) == os.path.abspath(destination):
            return {"published": False, "status": "PATH_COLLISION", "cleanup_succeeded": False}
    except (OSError, ValueError):
        return {"published": False, "status": "INVALID_PATH", "cleanup_succeeded": False}

    def cleanup() -> bool:
        try:
            if temporary.is_symlink() or temporary.exists():
                temporary.unlink()
            return True
        except OSError:
            return False

    try:
        if not destination.parent.exists() or not destination.parent.is_dir():
            return {"published": False, "status": "DESTINATION_DIRECTORY_MISSING", "cleanup_succeeded": cleanup()}
        try:
            temp_parent = temporary.parent.resolve(strict=True)
            dest_parent = destination.parent.resolve(strict=True)
        except OSError:
            return {"published": False, "status": "DESTINATION_DIRECTORY_MISSING", "cleanup_succeeded": cleanup()}
        if temp_parent != dest_parent:
            return {"published": False, "status": "TEMP_FILE_MUST_SHARE_DESTINATION_DIRECTORY", "cleanup_succeeded": cleanup()}
        if temporary.is_symlink():
            return {"published": False, "status": "TEMP_SYMLINK_REJECTED", "cleanup_succeeded": cleanup()}
        if not temporary.exists() or not temporary.is_file():
            return {"published": False, "status": "TEMP_OUTPUT_NOT_FOUND", "cleanup_succeeded": cleanup()}
        if destination.is_symlink():
            return {"published": False, "status": "DESTINATION_SYMLINK_REJECTED", "cleanup_succeeded": cleanup()}
        if destination.exists() and not destination.is_file():
            return {"published": False, "status": "DESTINATION_NOT_REGULAR_FILE", "cleanup_succeeded": cleanup()}

        validation = validator.validate_file(temporary, modality=modality)
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return {
                "published": False,
                "status": "TEMP_OUTPUT_INVALID",
                "validation_status": validation.get("status", "INVALID_VALIDATION_RESPONSE")
                if isinstance(validation, dict) else "INVALID_VALIDATION_RESPONSE",
                "cleanup_succeeded": cleanup(),
            }
        expected_hash = validation.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            return {"published": False, "status": "INVALID_VALIDATION_HASH", "cleanup_succeeded": cleanup()}

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags)
        with os.fdopen(descriptor, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                return {"published": False, "status": "TEMP_OUTPUT_NOT_REGULAR", "cleanup_succeeded": cleanup()}
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            final = os.fstat(stream.fileno())
            if any(
                getattr(final, field, None) != getattr(initial, field, None)
                for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
            ):
                return {"published": False, "status": "TEMP_OUTPUT_CHANGED", "cleanup_succeeded": cleanup()}
            if digest.hexdigest() != expected_hash:
                return {"published": False, "status": "TEMP_OUTPUT_CHANGED", "cleanup_succeeded": cleanup()}
            try:
                os.fsync(stream.fileno())
            except OSError:
                # Read-only descriptors cannot be fsynced on every platform. Hash and
                # metadata stability are still required before the atomic rename.
                pass

        os.replace(temporary, destination)
        return {
            "published": True,
            "status": "PUBLISHED",
            "name": destination.name,
            "sha256": expected_hash,
            "size_bytes": validation.get("size_bytes"),
            "modality": modality.casefold() if isinstance(modality, str) else modality,
            "cleanup_succeeded": True,
        }
    except (OSError, ValueError, TypeError) as exc:
        cleaned = cleanup()
        return {
            "published": False,
            "status": "ATOMIC_PUBLISH_FAILED",
            "error_type": type(exc).__name__,
            "cleanup_succeeded": cleaned,
        }
