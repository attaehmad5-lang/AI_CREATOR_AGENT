"""Fail-closed ClamAV adapter for provider-generated artifacts.

The scanner is optional at development time, but production deployments that
require malware scanning should inject this adapter and set
ProviderService(require_artifact_scan=True). No scanner output is returned to
callers, preventing paths or diagnostic text from leaking into API responses.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any, Callable


class ClamAVArtifactScanner:
    """Run ClamAV without a shell and return only a sanitized scan verdict."""

    def __init__(
        self,
        executable: str = "clamscan",
        *,
        timeout_seconds: float = 30.0,
        max_file_size_bytes: int = 1_000_000_000,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(executable, str) or not executable.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in executable
        ):
            raise ValueError("executable must be a non-empty command path")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 600
        ):
            raise ValueError("timeout_seconds must be finite and between 0 and 600")
        if (
            not isinstance(max_file_size_bytes, int)
            or isinstance(max_file_size_bytes, bool)
            or max_file_size_bytes < 1
        ):
            raise ValueError("max_file_size_bytes must be a positive integer")
        if runner is not None and not callable(runner):
            raise ValueError("runner must be callable")
        self.executable = executable
        self.timeout_seconds = float(timeout_seconds)
        self.max_file_size_bytes = max_file_size_bytes
        self.runner = runner or subprocess.run

    def scan(self, path: str | Path, *, modality: str) -> dict[str, Any]:
        if not isinstance(modality, str) or not modality.strip():
            return {"clean": False, "status": "INVALID_MODALITY"}
        if not isinstance(path, (str, Path)) or not str(path).strip() or "\x00" in str(path):
            return {"clean": False, "status": "INVALID_PATH"}
        artifact = Path(path)
        try:
            if artifact.is_symlink():
                return {"clean": False, "status": "SYMLINK_REJECTED"}
            if not artifact.is_file():
                return {"clean": False, "status": "ARTIFACT_NOT_FOUND"}
            size = artifact.stat().st_size
        except OSError:
            return {"clean": False, "status": "ARTIFACT_ACCESS_FAILED"}
        if size < 1:
            return {"clean": False, "status": "EMPTY_ARTIFACT"}
        if size > self.max_file_size_bytes:
            return {"clean": False, "status": "ARTIFACT_TOO_LARGE"}
        try:
            result = self.runner(
                [self.executable, "--no-summary", "--", str(artifact)],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return {"clean": False, "status": "SCANNER_UNAVAILABLE"}
        except subprocess.TimeoutExpired:
            return {"clean": False, "status": "SCAN_TIMEOUT"}
        except Exception as exc:
            return {"clean": False, "status": "SCANNER_EXECUTION_FAILED", "error_type": type(exc).__name__}
        return_code = getattr(result, "returncode", None)
        if return_code == 0:
            return {"clean": True, "status": "CLEAN"}
        if return_code == 1:
            return {"clean": False, "status": "MALWARE_DETECTED"}
        return {"clean": False, "status": "SCANNER_ERROR"}
