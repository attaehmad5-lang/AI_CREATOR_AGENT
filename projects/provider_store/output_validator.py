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
        max_json_depth: int = 128,
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
        self.min_size_bytes = min_size_bytes
        self.max_size_bytes = max_size_bytes
        self.max_json_depth = max_json_depth

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
                if normalized_modality in self.SIGNATURES:
                    mime = self._match_signature(normalized_modality, header)
                    if mime is None:
                        return {"valid": False, "status": "SIGNATURE_MISMATCH"}
                    container_status = self._validate_container(stream, normalized_modality, mime, size)
                    if container_status is not None:
                        return {"valid": False, "status": container_status}
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
        }

    @staticmethod
    def _validate_container(stream: Any, modality: str, mime: str, file_size: int) -> str | None:
        """Validate basic container boundaries for WAV, AVI and ISO-BMFF artifacts."""
        if mime == "audio/wav":
            stream.seek(0)
            header = stream.read(12)
            if (
                len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE"
                or int.from_bytes(header[4:8], "little") != file_size - 8
            ):
                return "MALFORMED_CONTAINER"
            offset = 12
            fmt_found = False
            data_found = False
            chunk_count = 0
            while offset < file_size:
                if offset + 8 > file_size or chunk_count >= 10000:
                    return "MALFORMED_CONTAINER"
                stream.seek(offset)
                chunk_header = stream.read(8)
                if len(chunk_header) != 8:
                    return "MALFORMED_CONTAINER"
                chunk_id = chunk_header[:4]
                chunk_size = int.from_bytes(chunk_header[4:8], "little")
                body_start = offset + 8
                body_end = body_start + chunk_size
                if body_end > file_size:
                    return "MALFORMED_CONTAINER"
                if chunk_id == b"fmt ":
                    if chunk_size < 16:
                        return "MALFORMED_CONTAINER"
                    stream.seek(body_start)
                    fmt = stream.read(16)
                    if len(fmt) != 16:
                        return "MALFORMED_CONTAINER"
                    channels = int.from_bytes(fmt[2:4], "little")
                    sample_rate = int.from_bytes(fmt[4:8], "little")
                    byte_rate = int.from_bytes(fmt[8:12], "little")
                    block_align = int.from_bytes(fmt[12:14], "little")
                    bits_per_sample = int.from_bytes(fmt[14:16], "little")
                    if not all((channels, sample_rate, byte_rate, block_align, bits_per_sample)):
                        return "MALFORMED_CONTAINER"
                    fmt_found = True
                elif chunk_id == b"data" and chunk_size > 0:
                    data_found = True
                offset = body_end + (chunk_size & 1)
                if offset > file_size:
                    return "MALFORMED_CONTAINER"
                chunk_count += 1
            return None if offset == file_size and fmt_found and data_found else "MALFORMED_CONTAINER"

        if mime == "video/avi":
            stream.seek(0)
            header = stream.read(12)
            if (
                len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"AVI "
                or int.from_bytes(header[4:8], "little") != file_size - 8
            ):
                return "MALFORMED_CONTAINER"
            return None

        if mime in {"video/mp4", "audio/mp4"}:
            offset = 0
            found: set[bytes] = set()
            box_count = 0
            while offset < file_size:
                if offset + 8 > file_size or box_count >= 10000:
                    return "MALFORMED_CONTAINER"
                stream.seek(offset)
                box_header = stream.read(8)
                if len(box_header) != 8:
                    return "MALFORMED_CONTAINER"
                size32 = int.from_bytes(box_header[:4], "big")
                box_type = box_header[4:8]
                header_size = 8
                if size32 == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return "MALFORMED_CONTAINER"
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif size32 == 0:
                    box_size = file_size - offset
                else:
                    box_size = size32
                if box_size < header_size or offset + box_size > file_size:
                    return "MALFORMED_CONTAINER"
                if box_type == b"ftyp" and box_size < header_size + 8:
                    return "MALFORMED_CONTAINER"
                if box_type in {b"moov", b"mdat"} and box_size <= header_size:
                    return "MALFORMED_CONTAINER"
                found.add(box_type)
                offset += box_size
                box_count += 1
                if size32 == 0 and offset != file_size:
                    return "MALFORMED_CONTAINER"
            if offset != file_size or not {b"ftyp", b"moov", b"mdat"}.issubset(found):
                return "MALFORMED_CONTAINER"
        return None

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
