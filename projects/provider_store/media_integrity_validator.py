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
        duration_seconds = None
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
                if box_type == "moov":
                    moov_end = offset + box_size
                    child_offset = offset + header_size
                    child_count = 0
                    while child_offset < moov_end:
                        child_count += 1
                        if child_count > 10000 or moov_end - child_offset < 8:
                            return {"valid": False, "status": "MP4_MOOV_STRUCTURE_INVALID", "size_bytes": size}
                        stream.seek(child_offset)
                        child_header = stream.read(8)
                        if len(child_header) != 8:
                            return {"valid": False, "status": "MP4_MOOV_STRUCTURE_INVALID", "size_bytes": size}
                        child_size = struct.unpack(">I", child_header[:4])[0]
                        child_type = child_header[4:8]
                        child_header_size = 8
                        if child_size == 1:
                            ext = stream.read(8)
                            if len(ext) != 8:
                                return {"valid": False, "status": "MP4_EXTENDED_BOX_HEADER_TRUNCATED", "size_bytes": size}
                            child_size = struct.unpack(">Q", ext)[0]
                            child_header_size = 16
                        elif child_size == 0:
                            child_size = moov_end - child_offset
                        if child_size < child_header_size or child_size > moov_end - child_offset:
                            return {"valid": False, "status": "MP4_MOOV_BOX_SIZE_INVALID", "size_bytes": size}
                        if child_type == b"mvhd":
                            payload_length = child_size - child_header_size
                            stream.seek(child_offset + child_header_size)
                            prefix = stream.read(min(payload_length, 32))
                            if len(prefix) < 20:
                                return {"valid": False, "status": "MP4_MVHD_TRUNCATED", "size_bytes": size}
                            version = prefix[0]
                            if version == 0:
                                timescale = struct.unpack(">I", prefix[12:16])[0]
                                duration = struct.unpack(">I", prefix[16:20])[0]
                            elif version == 1:
                                if len(prefix) < 32:
                                    return {"valid": False, "status": "MP4_MVHD_TRUNCATED", "size_bytes": size}
                                timescale = struct.unpack(">I", prefix[20:24])[0]
                                duration = struct.unpack(">Q", prefix[24:32])[0]
                            else:
                                return {"valid": False, "status": "MP4_MVHD_VERSION_UNSUPPORTED", "size_bytes": size}
                            if timescale == 0:
                                return {"valid": False, "status": "MP4_MVHD_TIMESCALE_INVALID", "size_bytes": size}
                            if duration != (0xFFFFFFFF if version == 0 else 0xFFFFFFFFFFFFFFFF):
                                duration_seconds = duration / timescale
                                if duration_seconds > self.max_duration_seconds:
                                    return {
                                        "valid": False, "status": "MEDIA_DURATION_EXCEEDED",
                                        "duration_seconds": duration_seconds, "size_bytes": size,
                                    }
                        child_offset += child_size
                boxes.append(box_type)
                offset += box_size
        if "ftyp" not in boxes or "mdat" not in boxes or not ({"moov", "moof"} & set(boxes)):
            return {"valid": False, "status": "MP4_REQUIRED_BOX_MISSING", "boxes": boxes, "size_bytes": size}
        result = {"valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "MP4", "boxes": boxes, "size_bytes": size}
        if duration_seconds is not None:
            result["duration_seconds"] = duration_seconds
        return result

    @staticmethod
    def _read_ebml_size(stream: Any, position: int, file_size: int) -> tuple[int, int, bool] | None:
        """Read an EBML size VINT as (value, width, unknown_size)."""
        if position < 0 or position >= file_size:
            return None
        stream.seek(position)
        first = stream.read(1)
        if len(first) != 1 or first[0] == 0:
            return None
        marker = 0x80
        width = 1
        while not (first[0] & marker) and width <= 8:
            marker >>= 1
            width += 1
        if width > 8 or position + width > file_size:
            return None
        tail = stream.read(width - 1)
        if len(tail) != width - 1:
            return None
        value = first[0] & (marker - 1)
        for byte in tail:
            value = (value << 8) | byte
        unknown_value = (1 << (7 * width)) - 1
        return value, width, value == unknown_value

    @staticmethod
    def _read_ebml_id(stream: Any, position: int, file_size: int) -> tuple[bytes, int] | None:
        if position < 0 or position >= file_size:
            return None
        stream.seek(position)
        first = stream.read(1)
        if len(first) != 1 or first[0] == 0:
            return None
        marker = 0x80
        width = 1
        while not (first[0] & marker) and width <= 4:
            marker >>= 1
            width += 1
        if width > 4 or position + width > file_size:
            return None
        tail = stream.read(width - 1)
        if len(tail) != width - 1:
            return None
        return first + tail, width

    def _validate_webm(self, path: Path, size: int) -> dict[str, Any]:
        with path.open("rb") as stream:
            # EBML header ID and bounded declared payload.
            if stream.read(4) != b"\\x1a\\x45\\xdf\\xa3":
                return {"valid": False, "status": "WEBM_EBML_HEADER_INVALID", "size_bytes": size}
            header_size_info = self._read_ebml_size(stream, 4, size)
            if header_size_info is None:
                return {"valid": False, "status": "WEBM_EBML_HEADER_SIZE_INVALID", "size_bytes": size}
            header_size, header_width, header_unknown = header_size_info
            if header_unknown:
                return {"valid": False, "status": "WEBM_EBML_HEADER_SIZE_UNKNOWN", "size_bytes": size}
            segment_id_offset = 4 + header_width + header_size
            if segment_id_offset > size or segment_id_offset + 4 > size:
                return {"valid": False, "status": "WEBM_EBML_HEADER_TRUNCATED", "size_bytes": size}
            stream.seek(segment_id_offset)
            if stream.read(4) != b"\\x18\\x53\\x80\\x67":
                return {"valid": False, "status": "WEBM_SEGMENT_MISSING", "size_bytes": size}
            segment_size_info = self._read_ebml_size(stream, segment_id_offset + 4, size)
            if segment_size_info is None:
                return {"valid": False, "status": "WEBM_SEGMENT_SIZE_INVALID", "size_bytes": size}
            segment_size, segment_width, segment_unknown = segment_size_info
            payload_offset = segment_id_offset + 4 + segment_width
            available = size - payload_offset
            if not segment_unknown and segment_size > available:
                return {"valid": False, "status": "WEBM_SEGMENT_SIZE_EXCEEDS_FILE", "size_bytes": size}
            segment_end = size if segment_unknown else payload_offset + segment_size
            if payload_offset >= segment_end:
                return {"valid": False, "status": "WEBM_SEGMENT_EMPTY", "size_bytes": size}
            element_id = self._read_ebml_id(stream, payload_offset, segment_end)
            if element_id is None:
                return {"valid": False, "status": "WEBM_SEGMENT_ELEMENT_INVALID", "size_bytes": size}
            _, element_id_width = element_id
            element_size_info = self._read_ebml_size(stream, payload_offset + element_id_width, segment_end)
            if element_size_info is None:
                return {"valid": False, "status": "WEBM_SEGMENT_ELEMENT_SIZE_INVALID", "size_bytes": size}
            element_size, element_size_width, element_unknown = element_size_info
            element_payload_offset = payload_offset + element_id_width + element_size_width
            element_available = segment_end - element_payload_offset
            if not element_unknown and element_size > element_available:
                return {"valid": False, "status": "WEBM_SEGMENT_ELEMENT_TRUNCATED", "size_bytes": size}
        return {
            "valid": True, "status": "MEDIA_CONTAINER_VALID", "container": "WEBM",
            "size_bytes": size, "segment_size": None if segment_unknown else segment_size,
        }

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
