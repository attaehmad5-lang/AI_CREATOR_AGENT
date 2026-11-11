"""Bounded, local media evidence verification for MasterJobEngine.

Physical file presence, size, SHA-256 and basic container signatures are checked.
This is NOT a substitute for decoder-based playback or perceptual quality checks.
Only use an artifact root owned by a trusted service account.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any


_ALLOWED = frozenset({
    "image/png", "image/jpeg", "image/webp", "audio/wav", "audio/mpeg",
    "video/mp4", "video/webm", "text/vtt", "text/srt", "text/plain",
})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FILE = 2 * 1024 * 1024 * 1024


class ArtifactIntegrityError(ValueError):
    """Artifact could not be independently validated; never report success."""


def _media_signature(media_type: str, prefix: bytes) -> bool:
    if media_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    if media_type == "audio/wav":
        return prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if media_type == "audio/mpeg":
        return prefix.startswith(b"ID3") or (len(prefix) > 1 and prefix[0] == 255 and prefix[1] & 224 == 224)
    if media_type == "video/mp4":
        return (len(prefix) >= 16 and int.from_bytes(prefix[:4], "big") >= 16
                and prefix[4:8] == b"ftyp")
    if media_type == "video/webm":
        return prefix.startswith(b"\x1a\x45\xdf\xa3")
    if media_type == "text/vtt":
        return prefix.removeprefix(b"\xef\xbb\xbf").startswith(b"WEBVTT")
    if media_type in ("text/srt", "text/plain"):
        return bool(prefix.strip()) and b"\x00" not in prefix
    return False


class ArtifactVerifier:
    """Validate relative paths, disallow symlinks, and stream content hashes.

    On POSIX, uses directory descriptors + O_NOFOLLOW for each component to
    prevent symlink substitutions between validation and open. On Windows,
    file roots must have restrictive OS ACLs; directory race exclusion cannot
    be guaranteed by pathlib alone.
    """

    def __init__(self, root: str | os.PathLike[str], *, max_file_bytes: int = _MAX_FILE):
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= _MAX_FILE:
            raise ArtifactIntegrityError("Invalid artifact size limit")
        base = Path(root)
        if base.is_symlink() or not base.is_dir():
            raise ArtifactIntegrityError("Artifact root must be a real directory")
        self.root = base.resolve(strict=True)
        self.max_file_bytes = max_file_bytes

    @staticmethod
    def _components(relative_path: str) -> tuple[str, ...]:
        if (not isinstance(relative_path, str) or not relative_path or len(relative_path) > 512
                or "\\" in relative_path or "\x00" in relative_path or ":" in relative_path):
            raise ArtifactIntegrityError("Unsafe artifact path")
        pure = PurePosixPath(relative_path)
        parts = relative_path.split("/")
        if pure.is_absolute() or any(part in ("", ".", "..") for part in parts):
            raise ArtifactIntegrityError("Unsafe artifact path")
        return tuple(parts)

    def _open_file(self, relative_path: str) -> int:
        parts = self._components(relative_path)
        if os.name == "posix":
            flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            dir_fd = os.open(self.root, flags_dir)
            try:
                for name in parts[:-1]:
                    next_fd = os.open(name, flags_dir, dir_fd=dir_fd)
                    os.close(dir_fd)
                    dir_fd = next_fd
                return os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
            except (OSError, ValueError) as exc:
                raise ArtifactIntegrityError("Cannot open artifact securely") from exc
            finally:
                os.close(dir_fd)
        target = self.root
        for name in parts:
            target = target / name
            if target.is_symlink():
                raise ArtifactIntegrityError("Artifact symlinks are not allowed")
        if not target.resolve(strict=True).is_relative_to(self.root):
            raise ArtifactIntegrityError("Artifact escaped root")
        try:
            return os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError("Cannot open artifact securely") from exc

    def inspect(self, relative_path: str, media_type: str) -> dict[str, Any]:
        if media_type not in _ALLOWED:
            raise ArtifactIntegrityError("Unsupported artifact media type")
        try:
            fd = self._open_file(relative_path)
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError("Artifact missing or unsafe") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= self.max_file_bytes:
                raise ArtifactIntegrityError("Artifact is empty, unsafe or oversized")
            digest = hashlib.sha256()
            prefix = b""
            with os.fdopen(fd, "rb", closefd=False) as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    if len(prefix) < 64:
                        prefix += chunk[:64-len(prefix)]
                    digest.update(chunk)
            if not _media_signature(media_type, prefix):
                raise ArtifactIntegrityError("Artifact header does not match media type")
            return {"path": relative_path, "media_type": media_type,
                    "size_bytes": info.st_size, "sha256": digest.hexdigest()}
        except OSError as exc:
            raise ArtifactIntegrityError("Artifact I/O failed") from exc
        finally:
            os.close(fd)

    def verify(self, evidence: Any, *, allowed_types: set[str] | frozenset[str]) -> bool:
        if not isinstance(evidence, dict) or set(evidence) != {"path", "media_type", "size_bytes", "sha256"}:
            raise ArtifactIntegrityError("Artifact evidence requires exact fields")
        if evidence.get("media_type") not in allowed_types:
            raise ArtifactIntegrityError("Artifact type not allowed for this stage")
        size = evidence.get("size_bytes")
        digest = evidence.get("sha256")
        if (type(size) is not int or not 0 < size <= self.max_file_bytes
                or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise ArtifactIntegrityError("Invalid artifact evidence")
        actual = self.inspect(evidence.get("path"), evidence["media_type"])
        if actual != evidence:
            raise ArtifactIntegrityError("Artifact hash or size mismatch")
        return True
