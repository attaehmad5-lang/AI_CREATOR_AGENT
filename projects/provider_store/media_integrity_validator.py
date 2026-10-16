"""Bounded-memory structural checks for common audio/video containers.

These checks validate container structure, not codecs or semantic media quality.
"""
from __future__ import annotations

import os
import struct
import wave
from pathlib import Path
from typing import Any


class MediaIntegrityValidator:
    """Validate common container structure without loading whole media files into RAM."""

    def __init__(self, *, max_bytes: int = 500_000_000, max_duration_seconds: int = 14400) -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 2_000_000_000:
            raise ValueError("max_bytes is outside safe limits")
        if isinstance(max_duration_seconds, bool) or not isinstance(max_duration_seconds, int) or not 1 <= max_duration_seconds <= 86400:
            raise ValueError("max_duration_seconds is outside safe limits")
        self.max_bytes = max_bytes
        self.max_duration_seconds = max_duration_seconds

    def validate(self, path: str | os.PathLike[str], *, modality: str) -> dict[str, Any]:
        if modality not in {"audio", "video"}:
            return {"valid": False, "status": "UNSUPPORTED_MEDIA_MODALITY"}
        try:
            file_path = Path(path)
            if not file_path.is_file():
                return {"valid": False, "status": "OUTPUT_FILE_MISSING_OR_EMPTY"}
            with file_path.open("rb") as stream:
                stat_size = os.fstat(stream.fileno()).st_size
                if stat_size <= 0:
                    return {"valid": False, "status": "OUTPUT_FILE_MISSING_OR_EMPTY"}
                if stat_size > self.max_bytes:
                    return {"valid": False, "status": "MEDIA_FILE_TOO_LARGE", "size_bytes": stat_size}
                header = stream.read(min(stat_size, 64))
            if modality == "audio" and header.startswith(b"RIFF") and header[8:12] == b"WAVE":
                return self._validate_wav(file_path, stat_size)
            if modality == "audio" and header.startswith(b"OggS"):
                return self._validate_ogg(file_path, stat_size)
            if modality == "video" and header[4:8] == b"ftyp":
                return self._validate_mp4(file_path, stat_size)
            if modality == "video" and header.startswith(b"\x1a\x45\xdf\xa3"):
                return self._validate_webm(file_path, stat_size)
            return {"valid": False, "status": "UNSUPPORTED_OR_INVALID_MEDIA_CONTAINER", "size_bytes": stat_size}
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
            frame_bytes = channels * sample_width
            remaining = frames
            while remaining:
                request_frames = min(remaining, max(1, 64 * 1024 // frame_bytes))
                chunk = audio.readframes(request_frames)
                if len(chunk) != request_frames * frame_bytes:
                    return {"valid": False, "status": "WAV_DATA_TRUNCATED", "size_bytes": size}
                remaining -= request_frames
        return {
            "valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "WAV",
            "size_bytes": size, "duration_seconds": duration,
            "channels": channels, "sample_rate": sample_rate,
        }

    def _validate_mp4(self, path: Path, size: int) -> dict[str, Any]:
        offset = 0
        boxes: list[str] = []
        with path.open("rb") as stream:
            while offset < size:
                if len(boxes) >= 10000 or size - offset < 8:
                    return {"valid": False, "status": "MP4_BOX_STRUCTURE_INVALID", "size_bytes": size}
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    return {"valid": False, "status": "MP4_BOX_STRUCTURE_INVALID", "size_bytes": size}
                box_size = struct.unpack(">I", header[:4])[0]
                try:
                    box_type = header[4:8].decode("ascii")
                except UnicodeDecodeError:
                    return {"valid": False, "status": "MP4_BOX_TYPE_INVALID", "size_bytes": size}
                header_size = 8
                if box_size == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return {"valid": False, "status": "MP4_EXTENDED_BOX_HEADER_TRUNCATED", "size_bytes": size}
                    box_size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or box_size > size - offset:
                    return {"valid": False, "status": "MP4_BOX_SIZE_INVALID", "size_bytes": size}
                boxes.append(box_type)
                offset += box_size
        if "ftyp" not in boxes or "mdat" not in boxes or not ({"moov", "moof"} & set(boxes)):
            return {"valid": False, "status": "MP4_REQUIRED_BOX_MISSING", "boxes": boxes, "size_bytes": size}
        return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "MP4", "boxes": boxes, "size_bytes": size}

    def _validate_webm(self, path: Path, size: int) -> dict[str, Any]:
        needle = b"\x18\x53\x80\x67"
        overlap = b""
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                block = overlap + chunk
                if needle in block:
                    return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "WEBM", "size_bytes": size}
                overlap = block[-(len(needle) - 1):]
        return {"valid": False, "status": "WEBM_SEGMENT_MISSING", "size_bytes": size}

    def _validate_ogg(self, path: Path, size: int) -> dict[str, Any]:
        offset = 0
        pages = 0
        with path.open("rb") as stream:
            while offset < size:
                if pages >= 10000 or size - offset < 27:
                    return {"valid": False, "status": "OGG_PAGE_STRUCTURE_INVALID", "size_bytes": size}
                stream.seek(offset)
                header = stream.read(27)
                if len(header) != 27 or header[:4] != b"OggS":
                    return {"valid": False, "status": "OGG_PAGE_STRUCTURE_INVALID", "size_bytes": size}
                if header[4] != 0:
                    return {"valid": False, "status": "OGG_VERSION_UNSUPPORTED", "size_bytes": size}
                segment_count = header[26]
                segment_table = stream.read(segment_count)
                if len(segment_table) != segment_count:
                    return {"valid": False, "status": "OGG_SEGMENT_TABLE_TRUNCATED", "size_bytes": size}
                body_size = sum(segment_table)
                page_end = offset + 27 + segment_count + body_size
                if page_end > size:
                    return {"valid": False, "status": "OGG_PAGE_TRUNCATED", "size_bytes": size}
                offset = page_end
                pages += 1
        if pages == 0:
            return {"valid": False, "status": "OGG_NO_PAGES", "size_bytes": size}
        return {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "OGG", "page_count": pages, "size_bytes": size}
