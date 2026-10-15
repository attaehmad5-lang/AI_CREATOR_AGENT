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
        max_image_dimension: int = 16384,
        max_image_pixels: int = 100_000_000,
        max_media_duration_seconds: float = 3600.0,
        allowed_audio_codecs: set[str] | None = None,
        allowed_video_codecs: set[str] | None = None,
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
        if (
            not isinstance(max_media_duration_seconds, (int, float))
            or isinstance(max_media_duration_seconds, bool)
            or not 0 < max_media_duration_seconds <= 86400
        ):
            raise ValueError("max_media_duration_seconds must be between 0 and 86400")
        self.max_image_dimension = max_image_dimension
        self.max_image_pixels = max_image_pixels
        self.max_media_duration_seconds = float(max_media_duration_seconds)
        self.allowed_audio_codecs = set(allowed_audio_codecs or {"pcm", "ieee_float", "alaw", "mulaw", "mp4a", "ac-3", "ec-3", "opus"})
        self.allowed_video_codecs = set(allowed_video_codecs or {"h264", "avc1", "avc3", "h265", "hvc1", "hev1", "vp9", "vp09", "av1", "av01", "mp4v", "mjpg", "xvid", "divx", "dib"})

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
    def _wav_properties(payload: bytes, total_size: int) -> dict[str, Any] | None:
        if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
            return None
        riff_size = int.from_bytes(payload[4:8], "little")
        if riff_size + 8 != total_size:
            return None
        index = 12
        fmt = None
        data_size = None
        while index + 8 <= len(payload):
            chunk_id = payload[index:index + 4]
            chunk_size = int.from_bytes(payload[index + 4:index + 8], "little")
            start = index + 8
            end = start + chunk_size
            if end > total_size or end < start:
                return None
            if chunk_id == b"fmt ":
                if chunk_size < 16 or end > len(payload):
                    return None
                fmt = {
                    "format": int.from_bytes(payload[start:start + 2], "little"),
                    "channels": int.from_bytes(payload[start + 2:start + 4], "little"),
                    "sample_rate": int.from_bytes(payload[start + 4:start + 8], "little"),
                    "byte_rate": int.from_bytes(payload[start + 8:start + 12], "little"),
                    "block_align": int.from_bytes(payload[start + 12:start + 14], "little"),
                    "bits_per_sample": int.from_bytes(payload[start + 14:start + 16], "little"),
                }
            elif chunk_id == b"data":
                data_size = chunk_size
            if chunk_id == b"data" and fmt is not None:
                break
            index = end + (chunk_size & 1)
        if fmt is None or data_size is None:
            return None
        channels, sample_rate = fmt["channels"], fmt["sample_rate"]
        bits, byte_rate = fmt["bits_per_sample"], fmt["byte_rate"]
        if not 1 <= channels <= 32 or not 8000 <= sample_rate <= 384000 or bits not in {8, 16, 24, 32, 64}:
            return None
        expected_align = channels * ((bits + 7) // 8)
        if fmt["block_align"] != expected_align or byte_rate != sample_rate * expected_align or data_size > total_size:
            return None
        codec = {1: "pcm", 3: "ieee_float", 6: "alaw", 7: "mulaw"}.get(fmt["format"])
        if codec is None or data_size == 0:
            return None
        return {
            "duration_seconds": data_size / byte_rate,
            "codec": codec,
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": bits,
        }

    @staticmethod
    def _flac_properties(payload: bytes) -> dict[str, Any] | None:
        if len(payload) < 42 or payload[:4] != b"fLaC":
            return None
        block_header = payload[4]
        if (block_header & 0x7F) != 0 or int.from_bytes(payload[5:8], "big") != 34:
            return None
        streaminfo = payload[8:42]
        packed = int.from_bytes(streaminfo[10:18], "big")
        sample_rate = packed >> 44
        channels = ((packed >> 41) & 0x7) + 1
        bits_per_sample = ((packed >> 36) & 0x1F) + 1
        total_samples = packed & ((1 << 36) - 1)
        if not 1 <= sample_rate <= 384000 or not 1 <= channels <= 8 or bits_per_sample not in {8, 16, 20, 24, 32} or total_samples <= 0:
            return None
        return {
            "duration_seconds": total_samples / sample_rate,
            "codec": "flac",
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": bits_per_sample,
        }

    @staticmethod
    def _ebml_element(payload: bytes, element_id: bytes) -> bytes | None:
        index = payload.find(element_id)
        if index < 0 or index + len(element_id) + 1 > len(payload):
            return None
        size_index = index + len(element_id)
        first = payload[size_index]
        mask = 0x80
        size_length = 1
        while size_length <= 8 and not first & mask:
            mask >>= 1
            size_length += 1
        if size_length > 8 or size_index + size_length > len(payload):
            return None
        value_size = first & (mask - 1)
        for byte in payload[size_index + 1:size_index + size_length]:
            value_size = (value_size << 8) | byte
        start = size_index + size_length
        end = start + value_size
        if end > len(payload):
            return None
        return payload[start:end]

    @classmethod
    def _webm_properties(cls, payload: bytes, modality: str) -> dict[str, Any] | None:
        if not payload.startswith(b"\x1a\x45\xdf\xa3"):
            return None
        scale_bytes = cls._ebml_element(payload, b"\x2a\xd7\xb1")
        duration_bytes = cls._ebml_element(payload, b"\x44\x89")
        if scale_bytes is None or duration_bytes not in (None, b""):
            # Duration is expected to be an IEEE-754 float, not an integer.
            pass
        if scale_bytes is None or duration_bytes is None or len(scale_bytes) not in range(1, 9) or len(duration_bytes) not in {4, 8}:
            return None
        scale = int.from_bytes(scale_bytes, "big")
        try:
            duration_units = __import__("struct").unpack(">f" if len(duration_bytes) == 4 else ">d", duration_bytes)[0]
        except Exception:
            return None
        duration = duration_units * scale / 1_000_000_000
        if scale <= 0 or not duration > 0:
            return None
        if modality == "video":
            codec_map = ((b"V_VP8", "vp8"), (b"V_VP9", "vp9"), (b"V_AV1", "av1"))
        else:
            codec_map = ((b"A_OPUS", "opus"), (b"A_VORBIS", "vorbis"))
        codec = next((name for token, name in codec_map if token in payload), None)
        if codec is None:
            return None
        result: dict[str, Any] = {"duration_seconds": duration, "codec": codec}
        if modality == "video":
            width_bytes = cls._ebml_element(payload, b"\xb0")
            height_bytes = cls._ebml_element(payload, b"\xba")
            if width_bytes is not None and height_bytes is not None:
                result["width"] = int.from_bytes(width_bytes, "big")
                result["height"] = int.from_bytes(height_bytes, "big")
        return result

    @staticmethod
    def _avi_properties(payload: bytes, total_size: int) -> dict[str, Any] | None:
        if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"AVI ":
            return None
        if int.from_bytes(payload[4:8], "little") + 8 != total_size:
            return None
        avih = payload.find(b"avih")
        if avih < 0 or avih + 8 + 40 > len(payload):
            return None
        header_size = int.from_bytes(payload[avih + 4:avih + 8], "little")
        start = avih + 8
        if header_size < 40 or start + 40 > len(payload):
            return None
        micros_per_frame = int.from_bytes(payload[start:start + 4], "little")
        total_frames = int.from_bytes(payload[start + 16:start + 20], "little")
        width = int.from_bytes(payload[start + 32:start + 36], "little")
        height = int.from_bytes(payload[start + 36:start + 40], "little")
        if micros_per_frame <= 0 or total_frames <= 0 or width <= 0 or height <= 0:
            return None
        codec = None
        index = 0
        while True:
            index = payload.find(b"strh", index)
            if index < 0:
                break
            if index + 16 <= len(payload):
                size = int.from_bytes(payload[index + 4:index + 8], "little")
                stream_type = payload[index + 8:index + 12]
                handler = payload[index + 12:index + 16]
                if size >= 8 and stream_type == b"vids":
                    try:
                        codec = handler.decode("ascii").strip().casefold()
                    except UnicodeDecodeError:
                        return None
                    break
            index += 4
        if not codec:
            return None
        return {
            "duration_seconds": total_frames * micros_per_frame / 1_000_000,
            "codec": codec,
            "width": width,
            "height": height,
        }

    @staticmethod
    def _mp4_properties(payload: bytes, total_size: int, modality: str) -> dict[str, Any] | None:
        if len(payload) < 16 or payload[4:8] != b"ftyp":
            return None
        # Require a bounded, complete moov box in the inspected header region.
        index = 0
        moov = None
        while index + 8 <= len(payload):
            box_size = int.from_bytes(payload[index:index + 4], "big")
            box_type = payload[index + 4:index + 8]
            header_size = 8
            if box_size == 1:
                if index + 16 > len(payload):
                    return None
                box_size = int.from_bytes(payload[index + 8:index + 16], "big")
                header_size = 16
            elif box_size == 0:
                box_size = total_size - index
            if box_size < header_size or index + box_size > total_size:
                return None
            if box_type == b"moov":
                if index + box_size > len(payload):
                    return None
                moov = payload[index:index + box_size]
                break
            if index + box_size > len(payload):
                break
            index += box_size
        if moov is None:
            return None
        mvhd = moov.find(b"mvhd")
        if mvhd < 0 or mvhd + 24 > len(moov):
            return None
        data = mvhd + 4
        version = moov[data]
        if version == 0:
            if data + 20 > len(moov):
                return None
            timescale = int.from_bytes(moov[data + 12:data + 16], "big")
            duration = int.from_bytes(moov[data + 16:data + 20], "big")
        elif version == 1:
            if data + 32 > len(moov):
                return None
            timescale = int.from_bytes(moov[data + 20:data + 24], "big")
            duration = int.from_bytes(moov[data + 24:data + 32], "big")
        else:
            return None
        stsd = moov.find(b"stsd")
        codec = None
        if stsd >= 0 and stsd + 20 <= len(moov):
            entry_type = moov[stsd + 16:stsd + 20]
            try:
                codec = entry_type.decode("ascii").casefold()
            except UnicodeDecodeError:
                return None
        if timescale <= 0 or duration <= 0 or not codec:
            return None
        return {"duration_seconds": duration / timescale, "codec": codec}

    def _media_properties(self, mime: str, payload: bytes, total_size: int, modality: str) -> dict[str, Any] | None:
        if mime == "audio/wav":
            return self._wav_properties(payload, total_size)
        if mime == "audio/flac":
            return self._flac_properties(payload)
        if mime == "audio/ogg":
            return None
        if mime == "video/webm":
            return self._webm_properties(payload, modality)
        if mime == "video/avi":
            return self._avi_properties(payload, total_size)
        if mime in {"audio/mp4", "video/mp4"}:
            return self._mp4_properties(payload, total_size, modality)
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
                media_properties = None
                if normalized_modality in self.SIGNATURES:
                    mime = self._match_signature(normalized_modality, header)
                    if mime is None:
                        return {"valid": False, "status": "SIGNATURE_MISMATCH"}
                    if normalized_modality == "image":
                        stream.seek(0)
                        image_header = stream.read(min(size, 1_048_576))
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
                    elif normalized_modality in {"audio", "video"}:
                        stream.seek(0)
                        media_payload = stream.read(min(size, 16 * 1024 * 1024))
                        if len(media_payload) != min(size, 16 * 1024 * 1024):
                            return {"valid": False, "status": "OUTPUT_CHANGED_DURING_VALIDATION"}
                        media_properties = self._media_properties(mime, media_payload, size, normalized_modality)
                        if media_properties is None:
                            return {"valid": False, "status": "MEDIA_PROPERTIES_UNVERIFIED"}
                        duration = media_properties.get("duration_seconds")
                        codec = media_properties.get("codec")
                        allowed_codecs = self.allowed_audio_codecs if normalized_modality == "audio" else self.allowed_video_codecs
                        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
                            return {"valid": False, "status": "MEDIA_DURATION_INVALID"}
                        if duration > self.max_media_duration_seconds:
                            return {"valid": False, "status": "MEDIA_DURATION_LIMIT_EXCEEDED", "duration_seconds": duration}
                        if not isinstance(codec, str) or codec.casefold() not in allowed_codecs:
                            return {"valid": False, "status": "MEDIA_CODEC_NOT_ALLOWED", "codec": codec}
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
            **(media_properties or {}),
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
