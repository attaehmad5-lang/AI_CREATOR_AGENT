"""Persistent, deterministic feature flags with project-scoped rollout."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_FLAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")


class ProviderFeatureFlagManager:
    def __init__(self, state_path: str | os.PathLike[str]) -> None:
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "flags": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "flags": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid feature flag state JSON") from exc
        flags = loaded.get("flags") if isinstance(loaded, dict) else None
        if not isinstance(flags, dict):
            raise ValueError("invalid feature flag state schema")
        for name, flag in flags.items():
            if (
                not isinstance(name, str) or not _FLAG_RE.fullmatch(name)
                or not isinstance(flag, dict)
                or not isinstance(flag.get("enabled"), bool)
                or isinstance(flag.get("rollout_percent"), bool)
                or not isinstance(flag.get("rollout_percent"), (int, float))
                or not 0 <= flag["rollout_percent"] <= 100
                or not isinstance(flag.get("allow_projects"), list)
                or not isinstance(flag.get("deny_projects"), list)
                or any(not isinstance(p, str) or not _PROJECT_RE.fullmatch(p) for p in flag["allow_projects"] + flag["deny_projects"])
            ):
                raise ValueError("invalid feature flag record")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.state_path)
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
        rollout_percent: float = 0,
        allow_projects: tuple[str, ...] | list[str] = (),
        deny_projects: tuple[str, ...] | list[str] = (),
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not _FLAG_RE.fullmatch(name):
            return {"status": "INVALID_FLAG_NAME", "saved": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_FLAG", "saved": False}
        if isinstance(rollout_percent, bool) or not isinstance(rollout_percent, (int, float)) or not 0 <= rollout_percent <= 100:
            return {"status": "INVALID_ROLLOUT_PERCENT", "saved": False}
        if not isinstance(allow_projects, (tuple, list)) or not isinstance(deny_projects, (tuple, list)):
            return {"status": "INVALID_PROJECT_SCOPE", "saved": False}
        if any(not isinstance(p, str) or not _PROJECT_RE.fullmatch(p) for p in list(allow_projects) + list(deny_projects)):
            return {"status": "INVALID_PROJECT_SCOPE", "saved": False}
        if len(set(allow_projects)) != len(allow_projects) or len(set(deny_projects)) != len(deny_projects):
            return {"status": "DUPLICATE_PROJECT_SCOPE", "saved": False}
        with self._transaction():
            self.data["flags"][name] = {
                "enabled": enabled,
                "rollout_percent": float(rollout_percent),
                "allow_projects": list(allow_projects),
                "deny_projects": list(deny_projects),
            }
            self._save()
        return {"status": "FEATURE_FLAG_SAVED", "saved": True, "flag": name}

    def evaluate(self, name: str, *, project_id: str, request_id: str) -> dict[str, Any]:
        if not isinstance(name, str) or not _FLAG_RE.fullmatch(name):
            return {"enabled": False, "status": "INVALID_FLAG_NAME"}
        if not isinstance(project_id, str) or not _PROJECT_RE.fullmatch(project_id):
            return {"enabled": False, "status": "INVALID_PROJECT_ID"}
        if not isinstance(request_id, str) or not _PROJECT_RE.fullmatch(request_id):
            return {"enabled": False, "status": "INVALID_REQUEST_ID"}
        with self._transaction():
            flag = self.data["flags"].get(name)
            if not isinstance(flag, dict):
                return {"enabled": False, "status": "FEATURE_FLAG_UNKNOWN", "flag": name}
            if flag["enabled"] is not True:
                return {"enabled": False, "status": "FEATURE_FLAG_DISABLED", "flag": name}
            if project_id in flag["deny_projects"]:
                return {"enabled": False, "status": "PROJECT_EXPLICITLY_DENIED", "flag": name}
            if project_id in flag["allow_projects"]:
                return {"enabled": True, "status": "PROJECT_ALLOWLISTED", "flag": name}
            percent = float(flag["rollout_percent"])
            if percent <= 0:
                return {"enabled": False, "status": "OUTSIDE_ROLLOUT", "flag": name}
            if percent >= 100:
                return {"enabled": True, "status": "ROLLOUT_ENABLED", "flag": name}
            digest = hashlib.sha256(f"{name}\0{project_id}\0{request_id}".encode()).digest()
            bucket = int.from_bytes(digest[:8], "big") % 10000
            enabled = bucket < int(percent * 100)
            return {
                "enabled": enabled,
                "status": "ROLLOUT_ENABLED" if enabled else "OUTSIDE_ROLLOUT",
                "flag": name,
                "bucket": bucket,
            }
