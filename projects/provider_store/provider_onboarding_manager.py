"""Approval, probe and enablement workflow for safely onboarding provider records."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class ProviderOnboardingManager:
    STATES = frozenset({"PENDING_REVIEW", "APPROVED", "VERIFIED", "ENABLED", "REJECTED"})
    _ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
    _SECRET_KEYS = ("token", "secret", "password", "api_key", "authorization", "credential", "private_key")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        register_disabled: Callable[..., Any],
        reviewer_authorizer: Callable[..., Any],
        probe_provider: Callable[..., Any],
        enable_provider: Callable[..., Any],
        max_requests: int = 5000,
        max_events: int = 10000,
    ) -> None:
        for value, name, maximum in ((max_requests, "max_requests", 50000), (max_events, "max_events", 100000)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside supported limits")
        for callback, name in (
            (register_disabled, "register_disabled"),
            (reviewer_authorizer, "reviewer_authorizer"),
            (probe_provider, "probe_provider"),
            (enable_provider, "enable_provider"),
        ):
            if not callable(callback):
                raise ValueError(f"{name} must be callable")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.register_disabled = register_disabled
        self.reviewer_authorizer = reviewer_authorizer
        self.probe_provider = probe_provider
        self.enable_provider = enable_provider
        self.max_requests = max_requests
        self.max_events = max_events
        self._thread_lock = threading.RLock()
        with self._transaction():
            pass

    @classmethod
    def _contains_secret_key(cls, value: Any) -> bool:
        if isinstance(value, dict):
            return any(
                (isinstance(key, str) and any(marker in key.casefold() for marker in cls._SECRET_KEYS))
                or cls._contains_secret_key(nested)
                for key, nested in value.items()
            )
        if isinstance(value, list):
            return any(cls._contains_secret_key(item) for item in value)
        return False

    @classmethod
    def _validate_provider(cls, provider: Any) -> dict[str, Any] | None:
        if not isinstance(provider, dict) or cls._contains_secret_key(provider):
            return None
        provider_id = provider.get("id")
        category = provider.get("category")
        docs_url = provider.get("docs_url")
        capabilities = provider.get("capabilities")
        allowed_hosts = provider.get("allowed_hosts")
        if (
            not isinstance(provider_id, str) or not cls._ID.fullmatch(provider_id)
            or not isinstance(category, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", category)
            or not isinstance(docs_url, str) or len(docs_url) > 2048
            or not isinstance(capabilities, list) or not capabilities
            or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", item) for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
            or not isinstance(allowed_hosts, list) or not allowed_hosts
            or any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) for host in allowed_hosts)
        ):
            return None
        try:
            parsed = urlsplit(docs_url)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            return None
        normalized = {
            "id": provider_id,
            "category": category,
            "docs_url": docs_url,
            "capabilities": sorted(capabilities),
            "allowed_hosts": sorted({host.rstrip(".").casefold() for host in allowed_hosts}),
            "adapter_api_version": provider.get("adapter_api_version", "1.0"),
        }
        version = normalized["adapter_api_version"]
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+", version):
            return None
        return normalized

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "requests": {}, "events": []}

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError("onboarding state cannot be read") from None
        if (
            not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("requests"), dict) or not isinstance(state.get("events"), list)
            or len(state["requests"]) > self.max_requests or len(state["events"]) > self.max_events
        ):
            raise ValueError("onboarding state schema is invalid")
        for request_id, record in state["requests"].items():
            if (
                not isinstance(request_id, str) or not self._ID.fullmatch(request_id)
                or not isinstance(record, dict) or record.get("state") not in self.STATES
                or self._validate_provider(record.get("provider")) is None
            ):
                raise ValueError("onboarding record is invalid")
        for event in state["events"]:
            if not isinstance(event, dict) or not all(
                isinstance(event.get(key), str) for key in ("request_id", "action", "status", "timestamp")
            ):
                raise ValueError("onboarding event is invalid")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    class _Transaction:
        def __init__(self, manager: "ProviderOnboardingManager") -> None:
            self.manager = manager
        def __enter__(self):
            self.manager._thread_lock.acquire()
            self.lock = interprocess_file_lock(self.manager.lock_path)
            self.lock.__enter__()
            try:
                self.state = self.manager._load()
            except Exception:
                self.lock.__exit__(None, None, None)
                self.manager._thread_lock.release()
                raise
            return self
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self.manager._save(self.state)
            finally:
                self.lock.__exit__(exc_type, exc, tb)
                self.manager._thread_lock.release()
            return False

    def _transaction(self):
        return self._Transaction(self)

    def _event(self, state: dict[str, Any], request_id: str, action: str, status: str, actor_id: str = "") -> None:
        state["events"].append({
            "request_id": request_id, "action": action, "status": status,
            "actor_id": actor_id, "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        state["events"] = state["events"][-self.max_events:]

    def submit(self, *, request_id: str, provider: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request_id, str) or not self._ID.fullmatch(request_id):
            return {"status": "INVALID_ONBOARDING_REQUEST_ID", "saved": False}
        normalized = self._validate_provider(provider)
        if normalized is None:
            return {"status": "INVALID_PROVIDER_ONBOARDING_METADATA", "saved": False}
        with self._transaction() as tx:
            state = tx.state
            if request_id in state["requests"]:
                return {"status": "ONBOARDING_REQUEST_EXISTS", "saved": False}
            if len(state["requests"]) >= self.max_requests:
                return {"status": "ONBOARDING_CAPACITY_REACHED", "saved": False}
        try:
            registered = self.register_disabled(provider=normalized, enabled=False)
        except Exception:
            return {"status": "PROVIDER_REGISTRATION_FAILED", "saved": False}
        if not isinstance(registered, dict) or registered.get("saved") is not True or registered.get("enabled", False) is not False:
            return {"status": "PROVIDER_REGISTRATION_FAILED", "saved": False}
        with self._transaction() as tx:
            if request_id in tx.state["requests"]:
                return {"status": "ONBOARDING_REQUEST_EXISTS", "saved": False}
            tx.state["requests"][request_id] = {
                "provider": normalized, "state": "PENDING_REVIEW",
                "reviewer_id": "", "verified_at": "", "enabled_at": "",
            }
            self._event(tx.state, request_id, "SUBMIT", "PENDING_REVIEW")
        return {"status": "ONBOARDING_SUBMITTED", "saved": True, "state": "PENDING_REVIEW"}

    def approve(self, *, request_id: str, reviewer_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not self._ID.fullmatch(request_id) or not isinstance(reviewer_id, str) or not self._ID.fullmatch(reviewer_id):
            return {"status": "INVALID_REVIEW_REQUEST", "approved": False}
        try:
            allowed = self.reviewer_authorizer(
                reviewer_id=reviewer_id, operation="provider.onboard.approve", request_id=request_id,
            )
        except Exception:
            return {"status": "REVIEW_AUTHORIZATION_FAILED", "approved": False}
        if allowed is not True:
            return {"status": "REVIEW_FORBIDDEN", "approved": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_REQUEST_NOT_FOUND", "approved": False}
            if record["state"] != "PENDING_REVIEW":
                return {"status": "INVALID_ONBOARDING_STATE", "approved": False}
            record["state"] = "APPROVED"
            record["reviewer_id"] = reviewer_id
            self._event(tx.state, request_id, "APPROVE", "APPROVED", reviewer_id)
        return {"status": "ONBOARDING_APPROVED", "approved": True}

    def verify(self, *, request_id: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
        if not isinstance(request_id, str) or not self._ID.fullmatch(request_id):
            return {"status": "INVALID_ONBOARDING_REQUEST_ID", "verified": False}
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 60:
            return {"status": "INVALID_TIMEOUT", "verified": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_REQUEST_NOT_FOUND", "verified": False}
            if record["state"] != "APPROVED":
                return {"status": "ONBOARDING_APPROVAL_REQUIRED", "verified": False}
            provider = dict(record["provider"])
        try:
            probe = self.probe_provider(provider=provider, timeout_seconds=float(timeout_seconds))
        except Exception:
            return {"status": "PROVIDER_PROBE_FAILED", "verified": False}
        if not isinstance(probe, dict) or probe.get("ok") is not True or probe.get("provider_id") != provider["id"]:
            return {"status": "PROVIDER_PROBE_UNVERIFIED", "verified": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict) or record["state"] != "APPROVED" or record["provider"] != provider:
                return {"status": "ONBOARDING_STATE_CHANGED", "verified": False}
            record["state"] = "VERIFIED"
            record["verified_at"] = datetime.now(timezone.utc).isoformat()
            self._event(tx.state, request_id, "VERIFY", "VERIFIED")
        return {"status": "PROVIDER_ONBOARDING_VERIFIED", "verified": True}

    def enable(self, *, request_id: str, actor_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not self._ID.fullmatch(request_id) or not isinstance(actor_id, str) or not self._ID.fullmatch(actor_id):
            return {"status": "INVALID_ENABLE_REQUEST", "enabled": False}
        try:
            allowed = self.reviewer_authorizer(
                reviewer_id=actor_id, operation="provider.onboard.enable", request_id=request_id,
            )
        except Exception:
            return {"status": "ENABLE_AUTHORIZATION_FAILED", "enabled": False}
        if allowed is not True:
            return {"status": "ENABLE_FORBIDDEN", "enabled": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_REQUEST_NOT_FOUND", "enabled": False}
            if record["state"] != "VERIFIED":
                return {"status": "LIVE_VERIFICATION_REQUIRED", "enabled": False}
            provider_id = record["provider"]["id"]
        try:
            enabled = self.enable_provider(provider_id=provider_id)
        except Exception:
            return {"status": "PROVIDER_ENABLE_FAILED", "enabled": False}
        if not isinstance(enabled, dict) or enabled.get("enabled") is not True:
            return {"status": "PROVIDER_ENABLE_FAILED", "enabled": False}
        with self._transaction() as tx:
            record = tx.state["requests"].get(request_id)
            if not isinstance(record, dict) or record["state"] != "VERIFIED":
                return {"status": "ONBOARDING_STATE_CHANGED", "enabled": False}
            record["state"] = "ENABLED"
            record["enabled_at"] = datetime.now(timezone.utc).isoformat()
            self._event(tx.state, request_id, "ENABLE", "ENABLED", actor_id)
        return {"status": "PROVIDER_ONBOARDING_ENABLED", "enabled": True}
