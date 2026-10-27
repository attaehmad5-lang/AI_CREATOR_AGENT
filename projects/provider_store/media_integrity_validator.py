"""Bounded structural integrity checks for common audio/video containers."""
from __future__ import annotations

import os
import struct
import wave
from pathlib import Path
from typing import Any


class MediaIntegrityValidator:
    """Validate container structure with standard-library parsing, not codec decoding."""

    def __init__(self, *, max_bytes: int = 500_000_000, max_duration_seconds: int = 14400) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 2_000_000_000:
            raise ValueError("max_bytes is outside safe limits")
        if isinstance(max_duration_seconds, bool) or not isinstance(max_duration_seconds, int) or not 1 <= max_duration_seconds <= 86400:
            raise ValueError("max_duration_seconds is outside safe limits")
        self.max_bytes = max_bytes
        self.max_duration_seconds = max_duration_seconds

    def validate(self, path: str | os.PathLike[str], *, modality: str) -> dict[str, Any]:
        # Validate untrusted arguments before set membership or filesystem calls:
        # malformed inputs must return a controlled result, never leak TypeError.
        if not isinstance(modality, str) or modality not in {"audio", "video"}:
            return {"valid": False, "status": "UNSUPPORTED_MEDIA_MODALITY"}
        if not isinstance(path, (str, os.PathLike)):
            return {"valid": False, "status": "INVALID_MEDIA_PATH"}
        # Path("") resolves to "."; reject blank textual paths before pathlib
        # normalizes them, otherwise the caller gets a misleading missing-output
        # status instead of the stable invalid-input result.
        if isinstance(path, str) and not path.strip():
            return {"valid": False, "status": "INVALID_MEDIA_PATH"}
        try:
            file_path = Path(path)
            size = file_path.stat().st_size
            if not file_path.is_file() or size <= 0:
                return {"valid": False, "status": "OUTPUT_FILE_MISSING_OR_EMPTY"}
            if size > self.max_bytes:
                return {"valid": False, "status": "MEDIA_FILE_TOO_LARGE", "size_bytes": size}
            with file_path.open("rb") as stream:
                header = stream.read(min(size, 64))
            if modality == "audio" and header.startswith(b"RIFF") and header[8:12] == b"WAVE":
                return self._validate_wav(file_path, size)
            if modality == "audio" and header.startswith(b"OggS"):
                return self._validate_ogg(file_path, size)
            if modality == "video" and header[4:8] == b"ftyp":
                return self._validate_mp4(file_path, size)
            if modality == "video" and header.startswith(b"\x1aE\xdf\xa3"):
                return self._validate_webm(file_path, size)
            return {"valid": False, "status": "UNSUPPORTED_OR_INVALID_MEDIA_CONTAINER", "size_bytes": size}
        except (OSError, EOFError, wave.Error, struct.error, ValueError) as exc:
            return {"valid": False, "status": "MEDIA_CONTAINER_INVALID", "error_type": type(exc).__name__}

    def _validate_wav(self, path: Path, size: int) -> dict[str, Any]:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
            if not 1 <= channels <= 32 or not 1 <= sample_width <= 4 or not 8000 <= sample_rate <= 384000 or frames <= 0:
                return {"valid": False, "status": "WAV_FORMAT_CONSTRAINT_FAILED", "size_bytes": size}
            duration = frames / sample_rate
            if duration > self.max_duration_seconds:
                return {"valid": False, "status": "MEDIA_DURATION_EXCEEDED", "duration_seconds": duration}
            expected_bytes = frames * channels * sample_width
            data = audio.readframes(frames)
            if len(data) != expected_bytes:
                return {"valid": False, "status": "WAV_DATA_TRUNCATED", "size_bytes": size}
        return {
            "valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "WAV",
            "size_bytes": size, "duration_seconds": duration,
            "channels": channels, "sample_rate": sample_rate,
        }

    def _validate_mp4(self, path: Path, size: int) -> dict[str, Any]:
        data = path.read_bytes()
        offset = 0
        boxes: list[str] = []
        while offset < len(data):
            if len(boxes) >= 10000 or len(data) - offset < 8:
                return {"valid": False, "status": "MP4_BOX_STRUCTURE_INVALID", "size_bytes": size}
            box_size = struct.unpack(">I", data[offset:offset + 4])[0]
            box_type_bytes = data[offset + 4:offset + 8]
            try:
                box_type = box_type_bytes.decode("ascii")
            except UnicodeDecodeError:
                return {"valid": False, "status": "MP4_BOX_TYPE_INVALID", "size_bytes": size}
            header_size = 8
            if box_size == 1:
                if len(data) - offset < 16:
                    return {"valid": False, "status": "MP4_EXTENDED_BOX_HEADER_TRUNCATED", "size_bytes": size}
                box_size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
                header_size = 16
            elif box_size == 0:
                box_size = len(data) - offset
            if box_size < header_size or box_size > len(data) - offset:
                return {"valid": False, "status": "MP4_BOX_SIZE_INVALID", "size_bytes": size}
            boxes.append(box_type)
            offset += box_size
        if "ftyp" not in boxes or "mdat" not in boxes or not ({"moov", "moof"} & set(boxes)):
            return {"valid": False, "status": "MP4_REQUIRED_BOX_MISSING", "boxes": boxes, "size_bytes": size}
        return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "MP4", "boxes": boxes, "size_bytes": size}

    def _validate_webm(self, path: Path, size: int) -> dict[str, Any]:
        data = path.read_bytes()
        if len(data) < 12 or b"\x18\x53\x80\x67" not in data[4:]:
            return {"valid": False, "status": "WEBM_SEGMENT_MISSING", "size_bytes": size}
        return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "WEBM", "size_bytes": size}

    def _validate_ogg(self, path: Path, size: int) -> dict[str, Any]:
        data = path.read_bytes()
        offset = 0
        pages = 0
        while offset < len(data):
            if pages >= 10000 or len(data) - offset < 27 or data[offset:offset + 4] != b"OggS":
                return {"valid": False, "status": "OGG_PAGE_STRUCTURE_INVALID", "size_bytes": size}
            if data[offset + 4] != 0:
                return {"valid": False, "status": "OGG_VERSION_UNSUPPORTED", "size_bytes": size}
            segment_count = data[offset + 26]
            header_end = offset + 27 + segment_count
            if header_end > len(data):
                return {"valid": False, "status": "OGG_SEGMENT_TABLE_TRUNCATED", "size_bytes": size}
            body_size = sum(data[offset + 27:header_end])
            page_end = header_end + body_size
            if page_end > len(data):
                return {"valid": False, "status": "OGG_PAGE_TRUNCATED", "size_bytes": size}
            offset = page_end
            pages += 1
        if pages == 0:
            return {"valid": False, "status": "OGG_NO_PAGES", "size_bytes": size}
        return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "OGG", "page_count": pages, "size_bytes": size}
