"""Local validation for provider-generated artifacts before they enter a project."""
from __future__ import annotations

import hashlib
import json
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

    def __init__(self, *, min_size_bytes: int = 1, max_size_bytes: int = 1_000_000_000) -> None:
        if (
            not isinstance(min_size_bytes, int)
            or isinstance(min_size_bytes, bool)
            or min_size_bytes < 1
            or not isinstance(max_size_bytes, int)
            or isinstance(max_size_bytes, bool)
            or max_size_bytes < min_size_bytes
        ):
            raise ValueError("output size limits must be valid positive integers")
        self.min_size_bytes = min_size_bytes
        self.max_size_bytes = max_size_bytes

    def validate_file(self, path: str | Path, *, modality: str) -> dict[str, Any]:
        """Validate existence, size, extension, signature/content, and compute SHA-256."""
        if not isinstance(modality, str) or modality.casefold() not in self.EXTENSIONS:
            return {"valid": False, "status": "UNSUPPORTED_MODALITY"}
        normalized_modality = modality.casefold()
        if not isinstance(path, (str, Path)) or not str(path).strip():
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
            size = artifact.stat().st_size
            if size < self.min_size_bytes:
                return {"valid": False, "status": "OUTPUT_TOO_SMALL", "size_bytes": size}
            if size > self.max_size_bytes:
                return {"valid": False, "status": "OUTPUT_TOO_LARGE", "size_bytes": size}
            with artifact.open("rb") as stream:
                header = stream.read(32)
                if normalized_modality in self.SIGNATURES:
                    mime = self._match_signature(normalized_modality, header)
                    if mime is None:
                        return {"valid": False, "status": "SIGNATURE_MISMATCH"}
                elif normalized_modality == "text":
                    stream.seek(0)
                    try:
                        stream.read().decode("utf-8")
                    except UnicodeDecodeError:
                        return {"valid": False, "status": "INVALID_TEXT_ENCODING"}
                    mime = "text/plain"
                else:
                    stream.seek(0)
                    try:
                        json.load(stream)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return {"valid": False, "status": "INVALID_JSON"}
                    mime = "application/json"
            digest = hashlib.sha256()
            with artifact.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
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

    @classmethod
    def _match_signature(cls, modality: str, header: bytes) -> str | None:
        for mime, signature, offset in cls.SIGNATURES[modality]:
            if header[offset:offset + len(signature)] != signature:
                continue
            if mime == "image/webp" and header[8:12] != b"WEBP":
                continue
            if mime in {"audio/wav", "video/avi"} and len(header) >= 12:
                expected = b"WAVE" if mime == "audio/wav" else b"AVI "
                if header[8:12] != expected:
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
