"""Persistent, deterministic feature flags for controlled provider rollout."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number


FEATURE_FLAG_PATH = Path(__file__).with_name("provider_feature_flags.json")
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,79}")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


class ProviderFeatureFlagManager:
    """Store flags atomically and evaluate rollout consistently across local processes."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, clock: Any = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(path) if path is not None else FEATURE_FLAG_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load()
        self._validate_document(self.data)

    def _load(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "flags": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("feature flag store is unreadable") from exc
        if not isinstance(data, dict):
            raise ValueError("feature flag store must be an object")
        return data

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("flags"), dict):
            raise ValueError("invalid feature flag store schema")
        if len(data["flags"]) > 1000:
            raise ValueError("feature flag store exceeds the flag limit")
        for name, flag in data["flags"].items():
            if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(flag, dict):
                raise ValueError("invalid feature flag entry")
            if set(flag) - {"enabled", "rollout_percent", "allowed_projects", "denied_projects", "expires_at"}:
                raise ValueError("unknown feature flag fields")
            if not isinstance(flag.get("enabled"), bool):
                raise ValueError("feature flag enabled must be boolean")
            rollout = finite_number(flag.get("rollout_percent"), minimum=0, maximum=100)
            if rollout is None:
                raise ValueError("feature flag rollout_percent must be between 0 and 100")
            for key in ("allowed_projects", "denied_projects"):
                values = flag.get(key, [])
                if not isinstance(values, list) or len(values) > 1000 or any(
                    not isinstance(value, str) or not _SCOPE.fullmatch(value) for value in values
                ) or len({value.casefold() for value in values}) != len(values):
                    raise ValueError(f"feature flag {key} is invalid")
            if set(flag.get("allowed_projects", [])) & set(flag.get("denied_projects", [])):
                raise ValueError("feature flag project allow/deny scopes overlap")
            expiry = flag.get("expires_at")
            if expiry is not None:
                if not isinstance(expiry, str):
                    raise ValueError("feature flag expires_at must be a timestamp")
                try:
                    parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("feature flag expires_at is invalid") from exc
                if parsed.tzinfo is None:
                    raise ValueError("feature flag expires_at must include a timezone")

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def set_flag(
        self,
        name: str,
        *,
        enabled: bool,
        rollout_percent: float = 100.0,
        allowed_projects: list[str] | None = None,
        denied_projects: list[str] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return {"saved": False, "status": "INVALID_FLAG_NAME"}
        if not isinstance(enabled, bool):
            return {"saved": False, "status": "INVALID_FLAG_STATE"}
        rollout = finite_number(rollout_percent, minimum=0, maximum=100)
        if rollout is None:
            return {"saved": False, "status": "INVALID_ROLLOUT_PERCENT"}
        flag = {
            "enabled": enabled,
            "rollout_percent": rollout,
            "allowed_projects": [] if allowed_projects is None else allowed_projects,
            "denied_projects": [] if denied_projects is None else denied_projects,
            "expires_at": expires_at,
        }
        candidate = {"version": "1.0", "flags": {name: flag}}
        try:
            self._validate_document(candidate)
        except ValueError:
            return {"saved": False, "status": "INVALID_FLAG_CONFIGURATION"}
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                current = self._load()
                self._validate_document(current)
            except ValueError:
                return {"saved": False, "status": "FLAG_STORE_INVALID"}
            current["flags"][name] = flag
            if len(current["flags"]) > 1000:
                return {"saved": False, "status": "FLAG_LIMIT_EXCEEDED"}
            try:
                self._save(current)
            except OSError as exc:
                return {"saved": False, "status": "FLAG_SAVE_FAILED", "error_type": type(exc).__name__}
            self.data = current
        return {"saved": True, "status": "FLAG_SAVED", "flag": name}

    def evaluate(self, name: str, *, project_id: str, subject_id: str | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return {"enabled": False, "status": "INVALID_FLAG_NAME"}
        if not isinstance(project_id, str) or not _SCOPE.fullmatch(project_id):
            return {"enabled": False, "status": "INVALID_PROJECT_ID"}
        if subject_id is not None and (not isinstance(subject_id, str) or not _SCOPE.fullmatch(subject_id)):
            return {"enabled": False, "status": "INVALID_SUBJECT_ID"}
        with self._lock, interprocess_file_lock(self.lock_path):
            try:
                current = self._load()
                self._validate_document(current)
            except ValueError:
                return {"enabled": False, "status": "FLAG_STORE_INVALID"}
            self.data = current
            flag = current["flags"].get(name)
            if not isinstance(flag, dict):
                return {"enabled": False, "status": "FLAG_NOT_FOUND"}
            if not flag["enabled"]:
                return {"enabled": False, "status": "FLAG_DISABLED"}
            if project_id in flag.get("denied_projects", []):
                return {"enabled": False, "status": "PROJECT_DENIED"}
            allowed = flag.get("allowed_projects", [])
            if allowed and project_id not in allowed:
                return {"enabled": False, "status": "PROJECT_NOT_ALLOWLISTED"}
            expiry = flag.get("expires_at")
            if expiry is not None:
                parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00")).astimezone(timezone.utc)
                now = self.clock()
                if not isinstance(now, datetime) or now.tzinfo is None:
                    return {"enabled": False, "status": "INVALID_CLOCK"}
                if now.astimezone(timezone.utc) >= parsed:
                    return {"enabled": False, "status": "FLAG_EXPIRED"}
            percent = float(flag["rollout_percent"])
            if percent <= 0:
                return {"enabled": False, "status": "OUTSIDE_ROLLOUT"}
            if percent < 100:
                subject = subject_id or project_id
                digest = hashlib.sha256(f"{name}:{project_id}:{subject}".encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:8], "big") % 10000
                if bucket >= int(percent * 100):
                    return {"enabled": False, "status": "OUTSIDE_ROLLOUT", "bucket": bucket}
                return {"enabled": True, "status": "FLAG_ENABLED", "bucket": bucket}
            return {"enabled": True, "status": "FLAG_ENABLED", "bucket": 0}
