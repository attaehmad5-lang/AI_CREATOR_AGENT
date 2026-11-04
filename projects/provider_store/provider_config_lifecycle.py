"""Versioned provider configuration migration and rollback-safe hot reload."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .encrypted_state_store import EncryptedJSONStateStore
from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .sensitive_data_policy import SensitiveDataPolicy


class ProviderConfigLifecycleManager:
    def __init__(
        self,
        validator: Callable[[dict[str, Any]], Any],
        *,
        max_history: int = 5,
        sensitive_data_policy: SensitiveDataPolicy | None = None,
        backup_encryption_key_provider: Any = None,
        backup_encryption_context: str = "provider-config-backup-v1",
    ) -> None:
        if not callable(validator):
            raise ValueError("validator must be callable")
        if isinstance(max_history, bool) or not isinstance(max_history, int) or not 1 <= max_history <= 100:
            raise ValueError("max_history is outside safe limits")
        if sensitive_data_policy is not None and not callable(getattr(sensitive_data_policy, "inspect", None)):
            raise ValueError("sensitive_data_policy must support inspect")
        if backup_encryption_key_provider is not None and not callable(backup_encryption_key_provider):
            raise ValueError("backup_encryption_key_provider must be callable")
        if (
            not isinstance(backup_encryption_context, str)
            or not backup_encryption_context
            or len(backup_encryption_context) > 96
            or any(ord(char) < 33 or ord(char) > 126 for char in backup_encryption_context)
        ):
            raise ValueError("backup_encryption_context is invalid")
        self.validator = validator
        self.sensitive_data_policy = sensitive_data_policy or SensitiveDataPolicy(
            blocked_categories=("credential", "payment_card")
        )
        self.backup_encryption_key_provider = backup_encryption_key_provider
        self.backup_encryption_context = backup_encryption_context
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
        except Exception as exc:
            return False, f"CONFIG_VALIDATION_FAILED:{type(exc).__name__}"
        try:
            scan = self.sensitive_data_policy.inspect(config)
        except Exception:
            return False, "SENSITIVE_DATA_SCAN_FAILED"
        if not isinstance(scan, dict) or scan.get("allowed") is not True:
            return False, (
                scan.get("status", "SENSITIVE_DATA_SCAN_FAILED")
                if isinstance(scan, dict) else "SENSITIVE_DATA_SCAN_FAILED"
            )
        try:
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
        try:
            initial_scan = self.sensitive_data_policy.inspect(config)
        except Exception:
            initial_scan = None
        if not isinstance(initial_scan, dict) or initial_scan.get("allowed") is not True:
            return {
                "status": (
                    initial_scan.get("status", "SENSITIVE_DATA_SCAN_FAILED")
                    if isinstance(initial_scan, dict) else "SENSITIVE_DATA_SCAN_FAILED"
                ),
                "migrated": False,
            }
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
        lock_path = config_path.with_name(config_path.name + ".lock")
        # Serialize the complete read/validate/backup/replace/reload transaction
        # across processes so concurrent migrations cannot overwrite each other.
        with interprocess_file_lock(lock_path):
            return self._migrate_file_locked(
                config_path, target_version=target_version, migrations=migrations
            )

    def _backup_path(self, config_path: Path) -> Path:
        suffix = ".bak.enc" if self.backup_encryption_key_provider is not None else ".bak"
        return config_path.with_name(config_path.name + suffix)

    def _backup_store(self, backup_path: Path) -> EncryptedJSONStateStore:
        if self.backup_encryption_key_provider is None:
            raise ValueError("encrypted config backup key provider is unavailable")
        # A path-derived context prevents ciphertext from being transplanted
        # between independently configured provider configuration files.
        path_digest = hashlib.sha256(str(backup_path.resolve()).encode("utf-8")).hexdigest()[:24]
        context = f"{self.backup_encryption_context}:{path_digest}"
        if len(context) > 128:
            raise ValueError("encrypted config backup context exceeds safe limits")
        return EncryptedJSONStateStore(
            backup_path,
            key_provider=self.backup_encryption_key_provider,
            context=context,
        )

    def _write_backup(self, config_path: Path, backup_path: Path, original: dict[str, Any]) -> None:
        if self.backup_encryption_key_provider is not None:
            self._backup_store(backup_path).write(original)
            return
        shutil.copy2(config_path, backup_path)

    def _restore_backup(self, config_path: Path, backup_path: Path) -> None:
        if self.backup_encryption_key_provider is None:
            shutil.copy2(backup_path, config_path)
            return
        original = self._backup_store(backup_path).read({})
        if not isinstance(original, dict):
            raise ValueError("encrypted config backup payload is invalid")
        fd_restore, restore_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.restore.", suffix=".tmp",
            dir=str(config_path.parent), text=True,
        )
        try:
            with os.fdopen(fd_restore, "w", encoding="utf-8") as stream:
                json.dump(original, stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(restore_name, config_path)
        except Exception:
            try:
                os.unlink(restore_name)
            except FileNotFoundError:
                pass
            raise

    def erase_encrypted_backup(
        self,
        path: str | os.PathLike[str],
        *,
        key_destroyer: Callable[[str], bool],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Cryptographically erase this config's encrypted migration backup.

        This requires the deployment callback to destroy the unique DEK for
        the path-bound backup context. A legacy plaintext .bak is never
        silently treated as covered by encrypted-backup erasure.
        """
        if self.backup_encryption_key_provider is None:
            return {
                "status": "CONFIG_BACKUP_ENCRYPTION_REQUIRED",
                "erased": False,
            }
        if not callable(key_destroyer):
            return {
                "status": "CONFIG_BACKUP_KEY_DESTROYER_UNAVAILABLE",
                "erased": False,
            }
        if confirmed is not True:
            return {
                "status": "CONFIG_BACKUP_ERASURE_CONFIRMATION_REQUIRED",
                "erased": False,
            }
        try:
            config_path = Path(path)
            backup_path = self._backup_path(config_path)
            legacy_plaintext_backup = config_path.with_name(config_path.name + ".bak")
            lock_path = config_path.with_name(config_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                if legacy_plaintext_backup.exists():
                    return {
                        "status": "CONFIG_PLAINTEXT_BACKUP_REQUIRES_MANUAL_CLEANUP",
                        "erased": False,
                    }
                result = self._backup_store(backup_path).erase(
                    {},
                    key_destroyer=key_destroyer,
                    confirmed=True,
                )
        except Exception as exc:
            return {
                "status": "CONFIG_BACKUP_ERASURE_FAILED",
                "erased": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict):
            return {
                "status": "INVALID_CONFIG_BACKUP_ERASURE_RESPONSE",
                "erased": False,
            }
        if result.get("status") == "STATE_CRYPTOGRAPHICALLY_ERASED":
            return {
                "status": "CONFIG_BACKUP_CRYPTOGRAPHICALLY_ERASED",
                "erased": True,
            }
        return {
            "status": result.get("status", "CONFIG_BACKUP_ERASURE_FAILED"),
            "erased": result.get("erased") is True,
            **(
                {"error_type": result["error_type"]}
                if isinstance(result.get("error_type"), str) else {}
            ),
        }

    def _migrate_file_locked(
        self,
        config_path: Path,
        *,
        target_version: str,
        migrations: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
    ) -> dict[str, Any]:
        if not config_path.is_file():
            return {"status": "CONFIG_FILE_NOT_FOUND", "migrated": False}
        try:
            with config_path.open("r", encoding="utf-8") as stream:
                original = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, OSError) as exc:
            return {"status": "INVALID_CONFIG_FILE", "migrated": False, "error_type": type(exc).__name__}
        result = self.migrate(original, target_version=target_version, migrations=migrations)
        if result.get("migrated") is not True:
            return result
        backup_path = self._backup_path(config_path)
        legacy_plaintext_backup = config_path.with_name(config_path.name + ".bak")
        if (
            self.backup_encryption_key_provider is not None
            and legacy_plaintext_backup.exists()
        ):
            return {
                "status": "CONFIG_PLAINTEXT_BACKUP_REQUIRES_MANUAL_CLEANUP",
                "migrated": False,
            }
        temp_name = None
        try:
            # Complete and durably verify the backup before allocating the new
            # configuration temp file; a failed KMS call cannot leak a file handle.
            self._write_backup(config_path, backup_path, original)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{config_path.name}.", suffix=".tmp",
                dir=str(config_path.parent), text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(result["config"], stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, config_path)
        except Exception as exc:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
            return {"status": "CONFIG_MIGRATION_WRITE_FAILED", "migrated": False, "error_type": type(exc).__name__}
        reloaded = self.reload(result["config"])
        if reloaded.get("reloaded") is not True:
            try:
                self._restore_backup(config_path, backup_path)
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
