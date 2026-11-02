"""Local validation for provider-generated artifacts before they enter a project."""
from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import stat
from pathlib import Path
from typing import Any


class ProviderOutputValidator:
    SIGNATURES: dict[str, tuple[tuple[str, bytes, int | None], ...]] = {
        "image": (
            ("image/png", b"\x89PNG\r\n\x1a\n", 0),
            ("image/jpeg", b"\xff\xd8\xff", 0),
            ("image/gif", b"GIF8", 0),
            ("image/webp", b"RIFF", 0),
        ),
        "audio": (
            ("audio/wav", b"RIFF", 0),
            ("audio/flac", b"fLaC", 0),
            ("audio/ogg", b"OggS", 0),
            ("audio/mpeg", b"ID3", 0),
        ),
        "video": (
            ("video/mp4", b"ftyp", 4),
            ("video/webm", b"\x1a\x45\xdf\xa3", 0),
            ("video/avi", b"RIFF", 0),
        ),
        "document": (("application/pdf", b"%PDF-", 0),),
    }

    EXTENSIONS: dict[str, set[str]] = {
        "image": {".png", ".jpg", ".jpeg", ".gif", ".webp"},
        "audio": {".wav", ".flac", ".ogg", ".mp3", ".m4a", ".aac"},
        "video": {".mp4", ".webm", ".avi", ".mov", ".m4v"},
        "document": {".pdf"},
        "text": {".txt", ".md", ".csv"},
        "json": {".json"},
    }

    def __init__(
        self, *, min_size_bytes: int = 1, max_size_bytes: int = 1_000_000_000,
        max_json_depth: int = 128, max_image_dimension: int = 32768,
        max_image_pixels: int = 100_000_000,
    ) -> None:
        if (
            not isinstance(min_size_bytes, int)
            or isinstance(min_size_bytes, bool)
            or min_size_bytes < 1
            or not isinstance(max_size_bytes, int)
            or isinstance(max_size_bytes, bool)
            or max_size_bytes < min_size_bytes
        ):
            raise ValueError("output size limits must be valid positive integers")
        if (
            not isinstance(max_json_depth, int)
            or isinstance(max_json_depth, bool)
            or max_json_depth < 1
            or max_json_depth > 4096
        ):
            raise ValueError("max_json_depth must be an integer between 1 and 4096")
        for label, value in (
            ("max_image_dimension", max_image_dimension),
            ("max_image_pixels", max_image_pixels),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self.min_size_bytes = min_size_bytes
        self.max_size_bytes = max_size_bytes
        self.max_json_depth = max_json_depth
        self.max_image_dimension = max_image_dimension
        self.max_image_pixels = max_image_pixels

    @staticmethod
    def _image_dimensions(stream: Any, mime: str, file_size: int) -> tuple[int, int] | None:
        """Read dimensions from bounded format headers; no image decompression is performed."""
        if mime == "image/png":
            if file_size < 24:
                return None
            stream.seek(8)
            chunk_length = stream.read(4)
            chunk_type = stream.read(4)
            dimensions = stream.read(8)
            if chunk_length == b"\\x00\\x00\\x00\\x0d" and chunk_type == b"IHDR" and len(dimensions) == 8:
                return int.from_bytes(dimensions[:4], "big"), int.from_bytes(dimensions[4:], "big")
            return None

        if mime == "image/gif":
            if file_size < 10:
                return None
            stream.seek(6)
            dimensions = stream.read(4)
            if len(dimensions) == 4:
                return int.from_bytes(dimensions[:2], "little"), int.from_bytes(dimensions[2:], "little")
            return None

        if mime == "image/jpeg":
            # JPEG dimensions are carried by a Start Of Frame segment. Inspect at
            # most the first MiB and never read a segment beyond the declared file.
            stream.seek(2)
            scan_limit = min(file_size, 1024 * 1024)
            sof_markers = {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }
            while stream.tell() < scan_limit:
                prefix = stream.read(1)
                if prefix != b"\\xff":
                    return None
                marker_byte = stream.read(1)
                while marker_byte == b"\\xff":
                    marker_byte = stream.read(1)
                if len(marker_byte) != 1:
                    return None
                marker = marker_byte[0]
                if marker in {0xD9, 0xDA}:
                    return None
                if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
                    continue
                raw_length = stream.read(2)
                if len(raw_length) != 2:
                    return None
                segment_length = int.from_bytes(raw_length, "big")
                segment_end = stream.tell() + segment_length - 2
                if segment_length < 2 or segment_end > file_size:
                    return None
                if marker in sof_markers:
                    frame = stream.read(min(segment_length - 2, 5))
                    if len(frame) < 5:
                        return None
                    height = int.from_bytes(frame[1:3], "big")
                    width = int.from_bytes(frame[3:5], "big")
                    return width, height
                stream.seek(segment_end)
            return None

        if mime == "image/webp":
            if file_size < 25:
                return None
            stream.seek(12)
            chunk_type = stream.read(4)
            chunk_size = int.from_bytes(stream.read(4), "little")
            if chunk_size < 5 or 20 + chunk_size > file_size:
                return None
            stream.seek(20)
            if chunk_type == b"VP8X":
                header = stream.read(10)
                if len(header) != 10:
                    return None
                width = 1 + int.from_bytes(header[4:7], "little")
                height = 1 + int.from_bytes(header[7:10], "little")
                return width, height
            if chunk_type == b"VP8 ":
                header = stream.read(10)
                if len(header) != 10 or header[3:6] != b"\\x9d\\x01\\x2a":
                    return None
                width = int.from_bytes(header[6:8], "little") & 0x3FFF
                height = int.from_bytes(header[8:10], "little") & 0x3FFF
                return width, height
            if chunk_type == b"VP8L":
                header = stream.read(5)
                if len(header) != 5 or header[0] != 0x2F:
                    return None
                width = 1 + header[1] + ((header[2] & 0x3F) << 8)
                height = 1 + ((header[2] >> 6) | (header[3] << 2) | ((header[4] & 0x0F) << 10))
                return width, height
            return None
        return None

    @staticmethod
    def _json_depth_within_limit(stream: Any, max_depth: int) -> bool:
        depth = 0
        in_string = False
        escaped = False
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            for byte in chunk:
                if in_string:
                    if escaped:
                        escaped = False
                    elif byte == 0x5C:
                        escaped = True
                    elif byte == 0x22:
                        in_string = False
                    continue
                if byte == 0x22:
                    in_string = True
                elif byte in (0x7B, 0x5B):
                    depth += 1
                    if depth > max_depth:
                        return False
                elif byte in (0x7D, 0x5D):
                    depth -= 1
        return True

    def validate_file(self, path: str | Path, *, modality: str) -> dict[str, Any]:
        """Validate existence, size, extension, signature/content, and compute SHA-256."""
        if not isinstance(modality, str) or modality.casefold() not in self.EXTENSIONS:
            return {"valid": False, "status": "UNSUPPORTED_MODALITY"}
        normalized_modality = modality.casefold()
        if (
            not isinstance(path, (str, Path))
            or not str(path).strip()
            or "\x00" in str(path)
        ):
            return {"valid": False, "status": "INVALID_PATH"}
        artifact = Path(path)
        if artifact.is_symlink():
            return {"valid": False, "status": "SYMLINK_REJECTED"}
        if not artifact.exists() or not artifact.is_file():
            return {"valid": False, "status": "OUTPUT_NOT_FOUND"}
        extension = artifact.suffix.casefold()
        if extension not in self.EXTENSIONS[normalized_modality]:
            return {"valid": False, "status": "EXTENSION_MISMATCH", "extension": extension}
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(artifact, flags)
            with os.fdopen(descriptor, "rb") as stream:
                file_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    return {"valid": False, "status": "INVALID_FILE_TYPE"}
                if not hasattr(os, "O_NOFOLLOW"):
                    path_stat = os.lstat(artifact)
                    if (
                        stat.S_ISLNK(path_stat.st_mode)
                        or path_stat.st_dev != file_stat.st_dev
                        or path_stat.st_ino != file_stat.st_ino
                    ):
                        return {"valid": False, "status": "SYMLINK_REJECTED"}
                size = file_stat.st_size
                if size < self.min_size_bytes:
                    return {"valid": False, "status": "OUTPUT_TOO_SMALL", "size_bytes": size}
                if size > self.max_size_bytes:
                    return {"valid": False, "status": "OUTPUT_TOO_LARGE", "size_bytes": size}
                header = stream.read(32)
                image_metadata: dict[str, Any] = {
                    "image_dimensions_validated": False,
                }
                if normalized_modality in self.SIGNATURES:
                    mime = self._match_signature(normalized_modality, header)
                    if mime is None:
                        return {"valid": False, "status": "SIGNATURE_MISMATCH"}
                    if normalized_modality == "image":
                        dimensions = self._image_dimensions(stream, mime, size)
                        if dimensions is not None:
                            width, height = dimensions
                            if width < 1 or height < 1:
                                return {"valid": False, "status": "INVALID_IMAGE_DIMENSIONS"}
                            if (
                                width > self.max_image_dimension
                                or height > self.max_image_dimension
                                or width * height > self.max_image_pixels
                            ):
                                return {
                                    "valid": False,
                                    "status": "IMAGE_DIMENSIONS_EXCEEDED",
                                    "width": width,
                                    "height": height,
                                }
                            image_metadata = {
                                "image_dimensions_validated": True,
                                "width": width,
                                "height": height,
                            }
                elif normalized_modality == "text":
                    stream.seek(0)
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    remaining = size
                    try:
                        while remaining:
                            chunk = stream.read(min(64 * 1024, remaining))
                            if not chunk:
                                return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                            decoder.decode(chunk)
                            remaining -= len(chunk)
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        return {"valid": False, "status": "INVALID_TEXT_ENCODING"}
                    if stream.read(1):
                        return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                    mime = "text/plain"
                else:
                    if size > 10_000_000:
                        return {"valid": False, "status": "OUTPUT_TOO_LARGE", "size_bytes": size}
                    stream.seek(0)
                    payload = stream.read(size + 1)
                    if len(payload) != size:
                        return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                    if not self._json_depth_within_limit(io.BytesIO(payload), self.max_json_depth):
                        return {"valid": False, "status": "JSON_NESTING_LIMIT"}
                    def reject_nonstandard_constant(token: str) -> None:
                        raise ValueError(f"non-standard JSON constant: {token}")
                    try:
                        json.loads(payload, parse_constant=reject_nonstandard_constant)
                    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
                        return {"valid": False, "status": "INVALID_JSON"}
                    mime = "application/json"
                digest = hashlib.sha256()
                stream.seek(0)
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                    digest.update(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                final_stat = os.fstat(stream.fileno())
                if any(
                    getattr(final_stat, field, None) != getattr(file_stat, field, None)
                    for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
                ):
                    return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
        except OSError:
            if artifact.is_symlink():
                return {"valid": False, "status": "SYMLINK_REJECTED"}
            return {"valid": False, "status": "OUTPUT_READ_FAILED"}
        return {
            "valid": True,
            "status": "VALID",
            "name": artifact.name,
            "modality": normalized_modality,
            "mime_type": mime,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            **(image_metadata if normalized_modality == "image" else {}),
        }

    @classmethod
    def _match_signature(cls, modality: str, header: bytes) -> str | None:
        for mime, signature, offset in cls.SIGNATURES[modality]:
            if header[offset:offset + len(signature)] != signature:
                continue
            if mime == "image/webp" and header[8:12] != b"WEBP":
                continue
            if mime in {"audio/wav", "video/avi"}:
                expected = b"WAVE" if mime == "audio/wav" else b"AVI "
                if len(header) < 12 or header[8:12] != expected:
                    continue
            return mime
        # MP3 files may omit ID3 and start with an MPEG audio frame sync.
        if modality == "audio" and len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
            return "audio/mpeg"
        # ISO base media files share the ftyp box, with compatible variants.
        if modality == "audio" and len(header) >= 8 and header[4:8] == b"ftyp":
            return "audio/mp4"
        if modality == "video" and len(header) >= 8 and header[4:8] == b"ftyp":
            return "video/mp4"
        return None
