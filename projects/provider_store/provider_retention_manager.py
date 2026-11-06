"""Durable, fail-closed provider data-retention and deletion enforcement.

Only opaque resource references and policy metadata are persisted; payloads and
credentials must never be passed to this manager. Deletion callbacks must be
idempotent for the supplied request key.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STATES = frozenset({"PENDING", "DELETION_FAILED", "DELETED"})


class ProviderRetentionManager:
    """Persist resource expiry and coordinate retryable provider-side deletion."""

    def __init__(self, state_path: str | os.PathLike[str], data_policy: Any) -> None:
        if not callable(getattr(data_policy, "evaluate", None)):
            raise ValueError("data_policy must support evaluate")
        self.path = Path(state_path)
        self.policy = data_policy
        self._lock = threading.RLock()
        self._state = self._load()

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and _ID.fullmatch(value) is not None

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "resources": {}}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("retention state exceeds size limit")
            state = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("retention state is unreadable") from exc
        if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("resources"), dict):
            raise ValueError("retention state schema is invalid")
        clean: dict[str, Any] = {}
        for key, item in state["resources"].items():
            if not self._valid_id(key) or not isinstance(item, dict):
                raise ValueError("retention resource record is invalid")
            required = {"provider_id", "tenant_id", "resource_ref", "created_at", "expires_at", "status", "attempts"}
            if set(item) != required:
                raise ValueError("retention resource fields are invalid")
            if not all(self._valid_id(item.get(k)) for k in ("provider_id", "tenant_id", "resource_ref")):
                raise ValueError("retention resource identifiers are invalid")
            if item.get("status") not in _STATES:
                raise ValueError("retention resource status is invalid")
            for field in ("created_at", "expires_at"):
                value = item.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 4102444800:
                    raise ValueError("retention timestamp is invalid")
            attempts = item.get("attempts")
            if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 100000:
                raise ValueError("retention attempts are invalid")
            if item["expires_at"] < item["created_at"]:
                raise ValueError("retention expiry precedes creation")
            clean[key] = dict(item)
        return {"version": 1, "resources": clean}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state, sort_keys=True, separators=(",", ":"))
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def register(
        self, *, resource_id: str, provider_id: str, tenant_id: str,
        resource_ref: str, data_categories: list[str] | tuple[str, ...],
        retention_days: int, provider_training_use: bool = False,
        user_training_consent: bool = False, now: float | None = None,
    ) -> dict[str, Any]:
        """Register an opaque provider resource only after policy approval."""
        if not all(self._valid_id(v) for v in (resource_id, provider_id, tenant_id, resource_ref)):
            return {"saved": False, "status": "INVALID_RESOURCE_IDENTIFIER"}
        current = time.time() if now is None else now
        if isinstance(current, bool) or not isinstance(current, (int, float)) or not 0 <= current <= 4102444800:
            return {"saved": False, "status": "INVALID_TIMESTAMP"}
        decision = self.policy.evaluate(
            provider_id=provider_id, data_categories=data_categories,
            retention_days=retention_days, provider_training_use=provider_training_use,
            user_training_consent=user_training_consent,
        )
        if not isinstance(decision, dict) or decision.get("allowed") is not True:
            status = decision.get("status") if isinstance(decision, dict) else None
            return {"saved": False, "status": "DATA_POLICY_REJECTED", "policy_status": status}
        record = {
            "provider_id": provider_id, "tenant_id": tenant_id,
            "resource_ref": resource_ref, "created_at": float(current),
            "expires_at": float(current) + retention_days * 86400,
            "status": "PENDING", "attempts": 0,
        }
        with self._lock:
            existing = self._state["resources"].get(resource_id)
            if existing is not None:
                if existing == record:
                    return {"saved": True, "status": "ALREADY_REGISTERED", "resource_id": resource_id}
                return {"saved": False, "status": "RESOURCE_ID_CONFLICT"}
            self._state["resources"][resource_id] = record
            try:
                self._save()
            except OSError:
                del self._state["resources"][resource_id]
                return {"saved": False, "status": "RETENTION_STATE_WRITE_FAILED"}
        return {"saved": True, "status": "RETENTION_REGISTERED", "resource_id": resource_id}

    def run_due(
        self, *, delete_provider_resource: Callable[..., Any],
        now: float | None = None, limit: int = 100,
    ) -> dict[str, Any]:
        """Attempt due deletions; failures remain queued for a later retry."""
        if not callable(delete_provider_resource):
            return {"status": "INVALID_DELETION_CALLBACK", "deleted": 0, "failed": 0}
        current = time.time() if now is None else now
        if isinstance(current, bool) or not isinstance(current, (int, float)) or not 0 <= current <= 4102444800:
            return {"status": "INVALID_TIMESTAMP", "deleted": 0, "failed": 0}
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            return {"status": "INVALID_LIMIT", "deleted": 0, "failed": 0}
        with self._lock:
            due = [
                (key, dict(value)) for key, value in self._state["resources"].items()
                if value["status"] in {"PENDING", "DELETION_FAILED"} and value["expires_at"] <= current
            ][:limit]
        deleted = failed = 0
        for resource_id, item in due:
            request_key = "retention-delete:" + resource_id
            try:
                result = delete_provider_resource(
                    provider_id=item["provider_id"], resource_ref=item["resource_ref"],
                    tenant_id=item["tenant_id"], idempotency_key=request_key,
                )
                success = result is True or (isinstance(result, dict) and result.get("deleted") is True)
            except Exception:
                success = False
            with self._lock:
                live = self._state["resources"].get(resource_id)
                if live is None or live["status"] == "DELETED":
                    continue
                live["attempts"] = min(live["attempts"] + 1, 100000)
                live["status"] = "DELETED" if success else "DELETION_FAILED"
                try:
                    self._save()
                except OSError:
                    # Never report deletion as durable when its state transition failed.
                    live["status"] = "DELETION_FAILED"
                    failed += 1
                    continue
            if success:
                deleted += 1
            else:
                failed += 1
        return {"status": "RETENTION_SWEEP_COMPLETE", "deleted": deleted, "failed": failed}

    def get_status(self, resource_id: str, *, tenant_id: str) -> dict[str, Any]:
        """Return only a tenant-scoped, non-secret status view."""
        if not self._valid_id(resource_id) or not self._valid_id(tenant_id):
            return {"status": "INVALID_RESOURCE_IDENTIFIER"}
        with self._lock:
            item = self._state["resources"].get(resource_id)
            if item is None or item["tenant_id"] != tenant_id:
                return {"status": "RESOURCE_NOT_FOUND"}
            return {
                "status": "RETENTION_STATUS",
                "resource_id": resource_id,
                "provider_id": item["provider_id"],
                "expires_at": item["expires_at"],
                "deletion_status": item["status"],
                "deletion_attempts": item["attempts"],
            }
