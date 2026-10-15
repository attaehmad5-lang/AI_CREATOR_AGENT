"""Local validation for provider-generated artifacts before they enter a project."""
from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import stat
import zlib
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
        max_image_dimension: int = 16384,
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
        if (
            not isinstance(max_image_dimension, int)
            or isinstance(max_image_dimension, bool)
            or max_image_dimension < 1
            or max_image_dimension > 1_000_000
            or not isinstance(max_image_pixels, int)
            or isinstance(max_image_pixels, bool)
            or max_image_pixels < 1
            or max_image_pixels > 10**12
        ):
            raise ValueError("image dimension limits must be positive bounded integers")
        self.min_size_bytes = min_size_bytes
        self.max_size_bytes = max_size_bytes
        self.max_json_depth = max_json_depth
        self.max_image_dimension = max_image_dimension
        self.max_image_pixels = max_image_pixels

    @staticmethod
    def _validate_png_structure(stream: Any, size: int) -> tuple[int, int] | None:
        """Validate PNG chunk ordering, lengths, CRCs, and required terminal chunks."""
        signature = b"\\x89PNG\\r\\n\\x1a\\n"
        stream.seek(0)
        if stream.read(8) != signature:
            return None
        offset = 8
        chunk_count = 0
        width = height = None
        seen_ihdr = seen_idat = seen_iend = False
        idat_ended = False
        while offset < size:
            chunk_count += 1
            if chunk_count > 100_000:
                return None
            stream.seek(offset)
            header = stream.read(8)
            if len(header) != 8:
                return None
            length = int.from_bytes(header[:4], "big")
            chunk_type = header[4:8]
            if (
                any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in chunk_type)
                or 65 <= chunk_type[2] <= 90 is False
            ):
                return None
            data_start = offset + 8
            crc_offset = data_start + length
            next_offset = crc_offset + 4
            if length > size or next_offset > size or next_offset <= offset:
                return None
            if not seen_ihdr:
                if chunk_type != b"IHDR" or length != 13:
                    return None
            elif chunk_type == b"IHDR":
                return None
            if chunk_type == b"IHDR":
                if seen_ihdr or offset != 8:
                    return None
                stream.seek(data_start)
                ihdr = stream.read(13)
                if len(ihdr) != 13:
                    return None
                width = int.from_bytes(ihdr[:4], "big")
                height = int.from_bytes(ihdr[4:8], "big")
                if ihdr[8] not in {1, 2, 4, 8, 16} and ihdr[8] != 8:
                    return None
                if ihdr[9] not in {0, 2, 3, 4, 6} or any(ihdr[10:13]):
                    return None
                seen_ihdr = True
            if chunk_type == b"IDAT":
                if idat_ended:
                    return None
                seen_idat = True
            elif seen_idat and chunk_type != b"IEND":
                idat_ended = True
            if chunk_type == b"IEND":
                if length != 0 or not seen_idat:
                    return None
                seen_iend = True
            crc = zlib.crc32(chunk_type)
            remaining = length
            stream.seek(data_start)
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    return None
                crc = zlib.crc32(chunk, crc)
                remaining -= len(chunk)
            stored_crc = stream.read(4)
            if len(stored_crc) != 4 or int.from_bytes(stored_crc, "big") != (crc & 0xFFFFFFFF):
                return None
            offset = next_offset
            if seen_iend:
                return (width, height) if offset == size and width is not None and height is not None else None
        return None

    @staticmethod
    def _image_dimensions(mime: str, payload: bytes) -> tuple[int, int] | None:
        """Read image dimensions from bounded format headers without decoding pixels."""
        if mime == "image/png":
            if len(payload) < 24 or payload[12:16] != b"IHDR":
                return None
            return int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")
        if mime == "image/gif":
            if len(payload) < 10:
                return None
            return int.from_bytes(payload[6:8], "little"), int.from_bytes(payload[8:10], "little")
        if mime == "image/jpeg":
            if len(payload) < 4 or payload[:2] != bytes((255, 216)):
                return None
            index = 2
            sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while index + 4 <= len(payload):
                if payload[index] != 0xFF:
                    index += 1
                    continue
                while index < len(payload) and payload[index] == 0xFF:
                    index += 1
                if index >= len(payload):
                    return None
                marker = payload[index]
                index += 1
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if index + 2 > len(payload):
                    return None
                segment_length = int.from_bytes(payload[index:index + 2], "big")
                if segment_length < 2 or index + segment_length > len(payload):
                    return None
                if marker in sof_markers:
                    if segment_length < 7:
                        return None
                    height = int.from_bytes(payload[index + 3:index + 5], "big")
                    width = int.from_bytes(payload[index + 5:index + 7], "big")
                    return width, height
                if marker == 0xDA:
                    return None
                index += segment_length
            return None
        if mime == "image/webp":
            if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WEBP":
                return None
            chunk_type = payload[12:16]
            if chunk_type == b"VP8X" and len(payload) >= 30:
                width = 1 + int.from_bytes(payload[24:27], "little")
                height = 1 + int.from_bytes(payload[27:30], "little")
                return width, height
            if chunk_type == b"VP8L" and len(payload) >= 25 and payload[20] == 0x2F:
                b1, b2, b3, b4 = payload[21:25]
                width = 1 + b1 + ((b2 & 0x3F) << 8)
                height = 1 + (b2 >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
                return width, height
            if chunk_type == b"VP8 " and len(payload) >= 30 and payload[23:26] == bytes((157, 1, 42)):
                width = int.from_bytes(payload[26:28], "little") & 0x3FFF
                height = int.from_bytes(payload[28:30], "little") & 0x3FFF
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
                image_dimensions = None
                if normalized_modality in self.SIGNATURES:
                    mime = self._match_signature(normalized_modality, header)
                    if mime is None:
                        return {"valid": False, "status": "SIGNATURE_MISMATCH"}
                    if normalized_modality == "image":
                        stream.seek(0)
                        image_header = stream.read(min(size, 1_048_576))
                        if mime == "image/png":
                            image_dimensions = self._validate_png_structure(stream, size)
                            if image_dimensions is None:
                                return {"valid": False, "status": "IMAGE_STRUCTURE_INVALID"}
                        else:
                            image_dimensions = self._image_dimensions(mime, image_header)
                        if image_dimensions is None:
                            return {"valid": False, "status": "IMAGE_DIMENSIONS_UNVERIFIED"}
                        width, height = image_dimensions
                        if width < 1 or height < 1:
                            return {"valid": False, "status": "IMAGE_DIMENSIONS_INVALID"}
                        if width > self.max_image_dimension or height > self.max_image_dimension:
                            return {"valid": False, "status": "IMAGE_DIMENSION_LIMIT_EXCEEDED", "width": width, "height": height}
                        if width * height > self.max_image_pixels:
                            return {"valid": False, "status": "IMAGE_PIXEL_LIMIT_EXCEEDED", "width": width, "height": height}
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
            **(
                {"width": image_dimensions[0], "height": image_dimensions[1]}
                if normalized_modality == "image" and image_dimensions is not None else {}
            ),
            "sha256": digest.hexdigest(),
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
