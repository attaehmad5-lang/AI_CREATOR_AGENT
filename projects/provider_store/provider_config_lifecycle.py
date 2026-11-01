"""Versioned provider configuration migration and rollback-safe hot reload."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry


class ProviderConfigLifecycleManager:
    def __init__(
        self,
        validator: Callable[[dict[str, Any]], Any],
        *,
        max_history: int = 5,
    ) -> None:
        if not callable(validator):
            raise ValueError("validator must be callable")
        if isinstance(max_history, bool) or not isinstance(max_history, int) or not 1 <= max_history <= 100:
            raise ValueError("max_history is outside safe limits")
        self.validator = validator
        self.max_history = max_history
        self._lock = threading.RLock()
        self.active_config: dict[str, Any] | None = None
        self.generation = 0
        self._history: deque[dict[str, Any]] = deque(maxlen=max_history)

    def _validate(self, config: Any) -> tuple[bool, str]:
        if not isinstance(config, dict):
            return False, "CONFIG_NOT_A_MAPPING"
        try:
            json.dumps(config, allow_nan=False)
            result = self.validator(copy.deepcopy(config))
        except Exception as exc:
            return False, f"CONFIG_VALIDATION_FAILED:{type(exc).__name__}"
        if result is True or (isinstance(result, dict) and result.get("valid") is True):
            return True, "CONFIG_VALID"
        return False, "CONFIG_VALIDATION_REJECTED"

    def migrate(
        self,
        config: dict[str, Any],
        *,
        target_version: str,
        migrations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    ) -> dict[str, Any]:
        if not isinstance(config, dict) or not isinstance(config.get("schema_version"), str):
            return {"status": "INVALID_SOURCE_CONFIG", "migrated": False}
        if not isinstance(target_version, str) or not target_version.strip():
            return {"status": "INVALID_TARGET_VERSION", "migrated": False}
        if not isinstance(migrations, dict):
            return {"status": "INVALID_MIGRATION_MAP", "migrated": False}
        candidate = copy.deepcopy(config)
        visited = set()
        steps = 0
        while candidate.get("schema_version") != target_version:
            source_version = candidate.get("schema_version")
            if source_version in visited or steps >= 20:
                return {"status": "MIGRATION_CYCLE_OR_LIMIT", "migrated": False}
            visited.add(source_version)
            migration = migrations.get(source_version)
            if not callable(migration):
                return {
                    "status": "MIGRATION_PATH_MISSING",
                    "migrated": False,
                    "source_version": source_version,
                    "target_version": target_version,
                }
            try:
                next_config = migration(copy.deepcopy(candidate))
            except Exception as exc:
                return {
                    "status": "MIGRATION_FAILED",
                    "migrated": False,
                    "error_type": type(exc).__name__,
                }
            if (
                not isinstance(next_config, dict)
                or not isinstance(next_config.get("schema_version"), str)
                or next_config.get("schema_version") == source_version
            ):
                return {"status": "INVALID_MIGRATION_RESULT", "migrated": False}
            valid, reason = self._validate(next_config)
            if not valid:
                return {"status": "MIGRATED_CONFIG_INVALID", "migrated": False, "reason": reason}
            candidate = copy.deepcopy(next_config)
            steps += 1
        valid, reason = self._validate(candidate)
        if not valid:
            return {"status": "MIGRATED_CONFIG_INVALID", "migrated": False, "reason": reason}
        return {
            "status": "CONFIG_MIGRATED",
            "migrated": True,
            "steps": steps,
            "config": candidate,
        }

    def reload(self, candidate: dict[str, Any]) -> dict[str, Any]:
        valid, reason = self._validate(candidate)
        if not valid:
            return {
                "status": "HOT_RELOAD_REJECTED",
                "reloaded": False,
                "reason": reason,
                "generation": self.generation,
            }
        with self._lock:
            if self.active_config is not None:
                self._history.append(copy.deepcopy(self.active_config))
            self.active_config = copy.deepcopy(candidate)
            self.generation += 1
            return {
                "status": "HOT_RELOAD_APPLIED",
                "reloaded": True,
                "generation": self.generation,
            }

    def rollback(self) -> dict[str, Any]:
        with self._lock:
            if not self._history:
                return {"status": "NO_CONFIG_ROLLBACK_AVAILABLE", "rolled_back": False}
            # Validate before mutating history so a rejected rollback preserves
            # both the active configuration and the rollback candidate.
            previous = self._history[-1]
            valid, reason = self._validate(previous)
            if not valid:
                return {"status": "ROLLBACK_CONFIG_INVALID", "rolled_back": False, "reason": reason}
            self._history.pop()
            if self.active_config is not None:
                self._history.append(copy.deepcopy(self.active_config))
            self.active_config = copy.deepcopy(previous)
            self.generation += 1
            return {
                "status": "CONFIG_ROLLED_BACK",
                "rolled_back": True,
                "generation": self.generation,
            }

    def migrate_file(
        self,
        path: str | os.PathLike[str],
        *,
        target_version: str,
        migrations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    ) -> dict[str, Any]:
        config_path = Path(path)
        if not config_path.is_file():
            return {"status": "CONFIG_FILE_NOT_FOUND", "migrated": False}
        try:
            with config_path.open("r", encoding="utf-8") as stream:
                original = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"status": "INVALID_CONFIG_FILE", "migrated": False, "error_type": type(exc).__name__}
        result = self.migrate(original, target_version=target_version, migrations=migrations)
        if result.get("migrated") is not True:
            return result
        backup_path = config_path.with_name(config_path.name + ".bak")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp",
            dir=str(config_path.parent), text=True,
        )
        try:
            shutil.copy2(config_path, backup_path)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result["config"], stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, config_path)
        except Exception as exc:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            return {"status": "CONFIG_MIGRATION_WRITE_FAILED", "migrated": False, "error_type": type(exc).__name__}
        reloaded = self.reload(result["config"])
        if reloaded.get("reloaded") is not True:
            try:
                fd_restore, restore_name = tempfile.mkstemp(
                    prefix=f".{config_path.name}.restore.", suffix=".tmp",
                    dir=str(config_path.parent), text=True,
                )
                os.close(fd_restore)
                shutil.copy2(backup_path, restore_name)
                atomic_replace_with_retry(restore_name, config_path)
            except Exception:
                return {
                    "status": "CONFIG_MIGRATION_ROLLBACK_FAILED",
                    "migrated": False,
                    "backup_path": str(backup_path),
                }
            return {
                "status": "CONFIG_MIGRATION_ROLLED_BACK",
                "migrated": False,
                "backup_path": str(backup_path),
            }
        return {
            "status": "CONFIG_MIGRATION_APPLIED",
            "migrated": True,
            "backup_path": str(backup_path),
            "generation": reloaded["generation"],
        }
