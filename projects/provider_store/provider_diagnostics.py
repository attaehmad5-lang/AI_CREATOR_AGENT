"""Secret-safe diagnostics for provider-manager JSON state files.

The diagnostic report contains file basenames, sizes, parse outcomes, and remediation
hints only. It never returns file contents, credential values, or raw parser errors.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

_COMPONENT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_MAX_FILE_BYTES = 1_048_576


class ProviderDiagnostics:
    """Inspect configured state files without mutating them or exposing their contents."""

    def inspect_files(
        self, files: Mapping[str, str | Path], *, max_file_bytes: int = _MAX_FILE_BYTES
    ) -> dict[str, Any]:
        if (
            not isinstance(files, Mapping)
            or not files
            or not isinstance(max_file_bytes, int)
            or isinstance(max_file_bytes, bool)
            or not 1 <= max_file_bytes <= _MAX_FILE_BYTES
        ):
            return {
                "status": "INVALID_DIAGNOSTIC_INPUT",
                "ready": False,
                "checks": [],
                "issue_count": 1,
            }

        checks: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for name, raw_path in files.items():
            if not isinstance(name, str) or not _COMPONENT_ID.fullmatch(name):
                return {
                    "status": "INVALID_DIAGNOSTIC_INPUT",
                    "ready": False,
                    "checks": [],
                    "issue_count": 1,
                }
            if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
                return {
                    "status": "INVALID_DIAGNOSTIC_INPUT",
                    "ready": False,
                    "checks": [],
                    "issue_count": 1,
                }

            path = Path(raw_path)
            identity = str(path.resolve(strict=False))
            if identity in seen_paths:
                checks.append({
                    "component": name,
                    "file": path.name,
                    "status": "DUPLICATE_PATH",
                    "valid": False,
                    "bytes": None,
                    "remediation": "Configure a distinct state-file path for each component.",
                })
                continue
            seen_paths.add(identity)

            base = {"component": name, "file": path.name, "bytes": None}
            try:
                if not path.exists():
                    checks.append({
                        **base, "status": "MISSING", "valid": False,
                        "remediation": "Create or restore the expected state file.",
                    })
                    continue
                if not path.is_file():
                    checks.append({
                        **base, "status": "NOT_A_FILE", "valid": False,
                        "remediation": "Configure a regular JSON state file.",
                    })
                    continue
                size = path.stat().st_size
                base["bytes"] = size
                if size > max_file_bytes:
                    checks.append({
                        **base, "status": "TOO_LARGE", "valid": False,
                        "remediation": "Review the file safely and reduce it below the configured size limit.",
                    })
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, (dict, list)):
                    checks.append({
                        **base, "status": "INVALID_ROOT", "valid": False,
                        "remediation": "Use a JSON object or array as the state-file root.",
                    })
                    continue
                checks.append({
                    **base, "status": "VALID_JSON", "valid": True,
                    "remediation": None,
                })
            except (OSError, UnicodeError):
                checks.append({
                    **base, "status": "UNREADABLE", "valid": False,
                    "remediation": "Check file permissions, encoding, and storage availability.",
                })
            except (json.JSONDecodeError, RecursionError, ValueError):
                checks.append({
                    **base, "status": "INVALID_JSON", "valid": False,
                    "remediation": "Repair or restore the JSON file from a trusted backup.",
                })

        issue_count = sum(not item["valid"] for item in checks)
        return {
            "status": "HEALTHY" if issue_count == 0 else "NEEDS_ATTENTION",
            "ready": issue_count == 0,
            "checks": checks,
            "issue_count": issue_count,
        }
