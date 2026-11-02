"""Atomic filesystem writes for generated provider outputs.

Temporary files are created beside the destination so os.replace stays on one
filesystem. A failed write/replace never truncates the previous successful output.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any


_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".gif": "GIF",
    ".webp": "WEBP",
}


def atomic_write_bytes(
    destination: str | os.PathLike[str],
    payload: bytes,
    *,
    max_bytes: int = 100 * 1024 * 1024,
) -> Path:
    """Atomically replace a file with bounded, non-empty bytes."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("output payload must be non-empty bytes")
    if len(payload) > max_bytes:
        raise ValueError("output payload exceeds the configured size limit")
    try:
        path = Path(destination)
    except (TypeError, ValueError):
        raise ValueError("invalid output destination") from None
    if not path.name or "\x00" in str(path):
        raise ValueError("invalid output destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("refusing to replace a symlink output")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck immediately before replacement; never follow a destination symlink.
        if path.is_symlink():
            raise ValueError("refusing to replace a symlink output")
        os.replace(temporary_path, path)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def atomic_save_image(
    image: Any,
    destination: str | os.PathLike[str],
    *,
    max_bytes: int = 50 * 1024 * 1024,
) -> Path:
    """Encode a Pillow-compatible image in memory, then atomically write it."""
    try:
        path = Path(destination)
    except (TypeError, ValueError):
        raise ValueError("invalid image output destination") from None
    image_format = _IMAGE_FORMATS.get(path.suffix.casefold())
    if image_format is None:
        raise ValueError("image output must use a supported extension")
    save = getattr(image, "save", None)
    if not callable(save):
        raise ValueError("image object does not support save()")
    buffer = io.BytesIO()
    save(buffer, format=image_format)
    return atomic_write_bytes(path, buffer.getvalue(), max_bytes=max_bytes)
