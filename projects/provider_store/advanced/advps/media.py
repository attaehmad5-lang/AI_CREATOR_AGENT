"""Media output metadata and byte-signature validation for provider responses."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Optional

from .errors import SecurityError, ValidationError

MIME_BY_TYPE = {
    "image": {"image/png", "image/jpeg", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "audio": {"audio/mpeg", "audio/wav", "audio/flac", "audio/ogg", "audio/webm"},
    "text": {"text/plain", "application/json"},
}


def _nonnegative_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _finite_nonnegative(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValidationError(f"{name} must be a finite non-negative number")
    return float(value)


def _signature_ok(data: bytes, mime_type: str) -> bool:
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime_type in {"video/mp4", "video/quicktime"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if mime_type in {"video/webm", "audio/webm"}:
        return data.startswith(b"\x1a\x45\xdf\xa3")
    if mime_type == "audio/wav":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if mime_type == "audio/flac":
        return data.startswith(b"fLaC")
    if mime_type == "audio/ogg":
        return data.startswith(b"OggS")
    if mime_type == "audio/mpeg":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if mime_type == "text/plain":
        try:
            data.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    if mime_type == "application/json":
        try:
            json.loads(data.decode("utf-8"))
            return True
        except (UnicodeDecodeError, ValueError):
            return False
    return False


def validate_media_payload(
    data: bytes,
    *,
    media_type: str,
    mime_type: str,
    max_bytes: int,
    expected_sha256: Optional[str] = None,
) -> dict:
    """Validate bounded bytes and a basic file signature; deep codec scanning is out of scope."""
    if not isinstance(media_type, str) or media_type not in MIME_BY_TYPE:
        raise ValidationError("unsupported media type")
    if not isinstance(mime_type, str) or mime_type not in MIME_BY_TYPE[media_type]:
        raise SecurityError("MIME type is not allowed for requested media type")
    max_bytes = _nonnegative_int(max_bytes, "max_bytes")
    if not isinstance(data, bytes):
        raise ValidationError("media payload must be bytes")
    if len(data) > max_bytes:
        raise SecurityError("media payload exceeds configured size limit")
    if not _signature_ok(data, mime_type):
        raise SecurityError("media bytes do not match declared MIME type")
    checksum = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None:
        if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(ch not in "0123456789abcdefABCDEF" for ch in expected_sha256)):
            raise ValidationError("expected_sha256 must be a 64-character hexadecimal digest")
        if checksum.casefold() != expected_sha256.casefold():
            raise SecurityError("media checksum mismatch")
    return {"media_type": media_type, "mime_type": mime_type, "size_bytes": len(data), "sha256": checksum}


def validate_media_metadata(
    metadata: dict,
    *,
    media_type: str,
    max_bytes: int,
    max_duration_seconds: Optional[float] = None,
    min_quality_score: Optional[float] = None,
    require_dimensions: bool = False,
) -> dict:
    """Validate provider-reported metadata; use validate_media_payload on actual bytes too."""
    if not isinstance(metadata, dict):
        raise ValidationError("media metadata must be an object")
    if not isinstance(media_type, str) or media_type not in MIME_BY_TYPE:
        raise ValidationError("unsupported media type")
    mime_type = metadata.get("mime_type")
    if not isinstance(mime_type, str) or mime_type not in MIME_BY_TYPE[media_type]:
        raise SecurityError("MIME type is not allowed for requested media type")
    size = _nonnegative_int(metadata.get("size_bytes"), "size_bytes")
    max_bytes = _nonnegative_int(max_bytes, "max_bytes")
    if size > max_bytes:
        raise SecurityError("media output exceeds configured size limit")
    duration = metadata.get("duration_seconds")
    if duration is not None:
        duration = _finite_nonnegative(duration, "duration_seconds")
    if max_duration_seconds is not None:
        max_duration_seconds = _finite_nonnegative(max_duration_seconds, "max_duration_seconds")
        if duration is None or duration > max_duration_seconds:
            raise SecurityError("media duration is missing or exceeds configured limit")
    width, height = metadata.get("width"), metadata.get("height")
    if require_dimensions and media_type == "image" and (width is None or height is None):
        raise ValidationError("image dimensions are required")
    if width is not None:
        width = _nonnegative_int(width, "width")
    if height is not None:
        height = _nonnegative_int(height, "height")
    quality = metadata.get("quality_score")
    if quality is not None:
        quality = _finite_nonnegative(quality, "quality_score")
        if quality > 100:
            raise ValidationError("quality_score must be between 0 and 100")
    if min_quality_score is not None:
        min_quality_score = _finite_nonnegative(min_quality_score, "min_quality_score")
        if min_quality_score > 100:
            raise ValidationError("min_quality_score must be <= 100")
        if quality is None or quality < min_quality_score:
            raise ValidationError("media output does not meet minimum quality score")
    checksum = metadata.get("sha256")
    if checksum is not None and (not isinstance(checksum, str) or len(checksum) != 64
                                 or any(ch not in "0123456789abcdefABCDEF" for ch in checksum)):
        raise ValidationError("sha256 must be a 64-character hexadecimal digest")
    return {
        "media_type": media_type, "mime_type": mime_type, "size_bytes": size,
        "duration_seconds": duration, "width": width, "height": height,
        "quality_score": quality, "sha256": checksum,
    }
