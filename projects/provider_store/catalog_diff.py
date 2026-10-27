"""Safe, dry-run-first catalog diff and application helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog_interchange import _read_valid_catalog, import_catalog


def diff_catalogs(
    current_path: str | Path,
    candidate_path: str | Path,
) -> dict[str, Any]:
    """Compare two valid catalogs without returning field values or credential material."""
    current = _read_valid_catalog(Path(current_path))
    candidate = _read_valid_catalog(Path(candidate_path))
    current_by_id = {item["id"]: item for item in current["providers"]}
    candidate_by_id = {item["id"]: item for item in candidate["providers"]}

    added = sorted(candidate_by_id.keys() - current_by_id.keys())
    removed = sorted(current_by_id.keys() - candidate_by_id.keys())
    changed = []
    for provider_id in sorted(current_by_id.keys() & candidate_by_id.keys()):
        before = current_by_id[provider_id]
        after = candidate_by_id[provider_id]
        changed_fields = sorted(
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        )
        if changed_fields:
            changed.append(
                {"id": provider_id, "changed_fields": changed_fields}
            )

    return {
        "status": "CATALOG_DIFF_READY",
        "valid": True,
        "current_version": current.get("version"),
        "candidate_version": candidate.get("version"),
        "added": added,
        "removed": removed,
        "changed": changed,
        "has_changes": bool(added or removed or changed or current.get("version") != candidate.get("version")),
    }


def apply_catalog_candidate(
    current_path: str | Path,
    candidate_path: str | Path,
    *,
    dry_run: bool = True,
    allow_removals: bool = False,
) -> dict[str, Any]:
    """Preview by default; apply only validated changes and explicitly approve removals."""
    if not isinstance(dry_run, bool) or not isinstance(allow_removals, bool):
        raise ValueError("dry_run and allow_removals must be booleans")
    diff = diff_catalogs(current_path, candidate_path)
    if dry_run:
        return {"status": "CATALOG_DIFF_DRY_RUN", "applied": False, "diff": diff}
    if diff["removed"] and not allow_removals:
        return {
            "status": "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED",
            "applied": False,
            "removed_provider_ids": diff["removed"],
            "diff": diff,
        }
    result = import_catalog(candidate_path, current_path, backup=True)
    if not result.get("imported"):
        return {"status": "CATALOG_DIFF_APPLY_FAILED", "applied": False, "diff": diff}
    return {
        "status": "CATALOG_DIFF_APPLIED",
        "applied": True,
        "provider_count": result["provider_count"],
        "diff": diff,
    }
