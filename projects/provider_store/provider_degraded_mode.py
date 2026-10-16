"""Explicit project/service degraded-mode policy with fail-closed execution gates."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_MODES = frozenset({"NORMAL", "DEGRADED", "READ_ONLY", "FAIL_CLOSED"})
_OPERATIONS = frozenset({"provider.generate", "provider.read", "provider.poll", "provider.cancel", "provider.verify"})
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_REASON_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")


class ProviderDegradedModeManager:
    """Persist explicit per-project/service operating modes and safe provider allowlists."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        max_policies: int = 20_000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(max_policies, bool) or not isinstance(max_policies, int) or not 1 <= max_policies <= 200_000:
            raise ValueError("max_policies is outside safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.path = Path(state_path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_policies = max_policies
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "policies": {}}
        with self._transaction():
            pass

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and _ID_RE.fullmatch(value) is not None

    def _now(self) -> str:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("degraded-mode clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": "1.0", "policies": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("degraded-mode state is unreadable") from None
        if not isinstance(loaded, dict) or loaded.get("version") != "1.0" or not isinstance(loaded.get("policies"), dict):
            raise ValueError("degraded-mode state schema is invalid")
        if len(loaded["policies"]) > self.max_policies:
            raise ValueError("degraded-mode state exceeds configured limit")
        for key, policy in loaded["policies"].items():
            if (
                not isinstance(key, str) or key.count("|") != 1
                or not all(self._valid_id(part) for part in key.split("|"))
                or not isinstance(policy, dict)
                or policy.get("mode") not in _MODES
                or not isinstance(policy.get("reason_code"), str)
                or not _REASON_RE.fullmatch(policy["reason_code"])
                or not isinstance(policy.get("allowed_providers"), list)
                or any(not self._valid_id(x) for x in policy["allowed_providers"])
                or len(set(policy["allowed_providers"])) != len(policy["allowed_providers"])
                or not isinstance(policy.get("updated_at"), str)
            ):
                raise ValueError("degraded-mode policy record is invalid")
            if policy["mode"] == "DEGRADED" and not policy["allowed_providers"]:
                raise ValueError("degraded mode requires an explicit provider allowlist")
        return loaded

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    class _Transaction:
        def __init__(self, manager: "ProviderDegradedModeManager") -> None:
            self.manager = manager
        def __enter__(self):
            self.manager._lock.acquire()
            self.lock = interprocess_file_lock(self.manager.lock_path)
            self.lock.__enter__()
            try:
                self.manager.data = self.manager._load()
            except Exception:
                self.lock.__exit__(None, None, None)
                self.manager._lock.release()
                raise
            return self.manager
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.manager._save()
            finally:
                self.lock.__exit__(exc_type, exc, tb)
                self.manager._lock.release()
            return False

    def _transaction(self):
        return self._Transaction(self)

    def set_mode(
        self,
        *,
        project_id: str,
        service_id: str,
        mode: str,
        reason_code: str,
        allowed_providers: list[str] | None = None,
    ) -> dict[str, Any]:
        providers = [] if allowed_providers is None else allowed_providers
        if (
            not self._valid_id(project_id) or not self._valid_id(service_id)
            or mode not in _MODES or not isinstance(reason_code, str)
            or not _REASON_RE.fullmatch(reason_code)
            or not isinstance(providers, list)
            or any(not self._valid_id(provider) for provider in providers)
            or len(set(providers)) != len(providers)
            or (mode == "DEGRADED" and not providers)
            or (mode != "DEGRADED" and providers)
        ):
            return {"status": "INVALID_DEGRADED_MODE_POLICY", "saved": False}
        try:
            updated_at = self._now()
        except Exception:
            return {"status": "DEGRADED_MODE_CLOCK_FAILED", "saved": False}
        key = f"{project_id}|{service_id}"
        with self._transaction():
            if key not in self.data["policies"] and len(self.data["policies"]) >= self.max_policies:
                return {"status": "POLICY_LIMIT_REACHED", "saved": False}
            self.data["policies"][key] = {
                "project_id": project_id, "service_id": service_id, "mode": mode,
                "reason_code": reason_code, "allowed_providers": sorted(providers),
                "updated_at": updated_at,
            }
        return {"status": "DEGRADED_MODE_UPDATED", "saved": True, "mode": mode}

    def evaluate(
        self,
        *,
        project_id: str,
        service_id: str,
        operation: str,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            not self._valid_id(project_id) or not self._valid_id(service_id)
            or operation not in _OPERATIONS
            or (provider_id is not None and not self._valid_id(provider_id))
        ):
            return {"allowed": False, "status": "INVALID_DEGRADED_MODE_REQUEST"}
        key = f"{project_id}|{service_id}"
        with self._transaction():
            policy = self.data["policies"].get(key)
            if not isinstance(policy, dict):
                return {"allowed": False, "status": "DEGRADED_MODE_NOT_CONFIGURED"}
            mode = policy["mode"]
            allowed = False
            if mode == "NORMAL":
                allowed = True
            elif mode == "DEGRADED":
                if operation in {"provider.read", "provider.poll", "provider.cancel"}:
                    allowed = True
                elif operation == "provider.generate":
                    allowed = (
                        provider_id in policy["allowed_providers"]
                        if provider_id is not None else bool(policy["allowed_providers"])
                    )
            elif mode == "READ_ONLY":
                allowed = operation in {"provider.read", "provider.poll", "provider.cancel"}
            elif mode == "FAIL_CLOSED":
                allowed = False
            return {
                "allowed": allowed,
                "status": "OPERATION_ALLOWED" if allowed else "OPERATION_BLOCKED_BY_DEGRADED_MODE",
                "mode": mode, "reason_code": policy["reason_code"],
                "provider_check_required": mode == "DEGRADED" and operation == "provider.generate" and provider_id is None,
            }
