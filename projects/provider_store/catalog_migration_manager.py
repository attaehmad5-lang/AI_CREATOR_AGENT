"""Explicit, fail-closed provider catalog schema migration helpers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .connection_manager import ConnectionManager
from .interprocess_lock import interprocess_file_lock


class ProviderCatalogMigrationManager:
    """Migrate known provider catalog versions without silently enabling unsafe providers."""

    CURRENT_VERSION = "3.0"
    SUPPORTED_SOURCE_VERSIONS = {"2.0"}

    @classmethod
    def migrate_file(
        cls,
        path: str | os.PathLike[str],
        *,
        backup_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        target = Path(path)
        backup = Path(backup_path) if backup_path is not None else target.with_name(target.name + ".v2.bak")
        if target.resolve() == backup.resolve():
            return {"status": "BACKUP_EQUALS_TARGET", "migrated": False}
        lock_path = target.with_name(target.name + ".lock")
        with interprocess_file_lock(lock_path):
            try:
                original_bytes = target.read_bytes()
            except OSError as exc:
                return {"status": "CATALOG_READ_FAILED", "migrated": False, "error_type": type(exc).__name__}
            try:
                original = json.loads(original_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
            if not isinstance(original, dict) or not isinstance(original.get("providers"), list):
                return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
            version = str(original.get("version", ""))
            if version == cls.CURRENT_VERSION:
                validation = ConnectionManager.validate_catalog_schema(original)
                if validation.get("valid") is True:
                    return {"status": "ALREADY_CURRENT", "migrated": False, "provider_count": len(original["providers"])}
                return {"status": "CURRENT_CATALOG_INVALID", "migrated": False, "errors": validation.get("errors", [])}
            if version not in cls.SUPPORTED_SOURCE_VERSIONS:
                return {"status": "UNSUPPORTED_SOURCE_VERSION", "migrated": False, "source_version": version}

            candidate = json.loads(json.dumps(original))
            seen: set[str] = set()
            disabled_for_safety = 0
            for provider in candidate["providers"]:
                if not isinstance(provider, dict):
                    return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
                provider_id = provider.get("id")
                if not isinstance(provider_id, str) or not provider_id.strip() or provider_id.casefold() in seen:
                    return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
                seen.add(provider_id.casefold())
                if "enabled" in provider and not isinstance(provider["enabled"], bool):
                    return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
                if "subscription_required" in provider and not isinstance(provider["subscription_required"], bool):
                    return {"status": "INVALID_SOURCE_CATALOG", "migrated": False}
                # Never infer enablement from a legacy display status.
                provider.setdefault("enabled", False)
                # Unknown billing semantics must default to paid/approval-required.
                provider.setdefault("subscription_required", True)
                provider.setdefault("capabilities", [])
                provider.setdefault("allowed_hosts", [])
                connection_type = str(provider.get("connection_type", "API")).upper()
                required_api_metadata_missing = (
                    connection_type == "API"
                    and (
                        not isinstance(provider.get("official_api_url"), str)
                        or not provider["official_api_url"].startswith("https://")
                        or not provider.get("allowed_hosts")
                        or not provider.get("capabilities")
                    )
                )
                if provider["enabled"] and required_api_metadata_missing:
                    provider["enabled"] = False
                    provider["status"] = "LOCKED"
                    disabled_for_safety += 1

            candidate["version"] = cls.CURRENT_VERSION
            validation = ConnectionManager.validate_catalog_schema(candidate)
            if validation.get("valid") is not True:
                return {
                    "status": "MIGRATION_VALIDATION_FAILED",
                    "migrated": False,
                    "errors": validation.get("errors", []),
                }

            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    try:
                        existing_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
                    except OSError as exc:
                        return {"status": "BACKUP_READ_FAILED", "migrated": False, "error_type": type(exc).__name__}
                    if existing_digest != hashlib.sha256(original_bytes).hexdigest():
                        return {"status": "BACKUP_CONFLICT", "migrated": False}
                else:
                    # Exclusive creation prevents overwriting an earlier recovery point.
                    with backup.open("xb") as stream:
                        stream.write(original_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                ConnectionManager._atomic_write_json(target, candidate)
            except OSError as exc:
                return {
                    "status": "MIGRATION_WRITE_FAILED",
                    "migrated": False,
                    "error_type": type(exc).__name__,
                    "backup_created": backup.exists(),
                }
            return {
                "status": "MIGRATED",
                "migrated": True,
                "source_version": version,
                "target_version": cls.CURRENT_VERSION,
                "provider_count": len(candidate["providers"]),
                "disabled_for_safety": disabled_for_safety,
                "backup_path": str(backup),
            }
