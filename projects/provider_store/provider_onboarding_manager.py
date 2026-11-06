"""Approval-gated provider onboarding with a verified probe before activation.

This module stores metadata only. It never stores credentials, imports provider
code, purchases subscriptions, or treats catalog registration as proof of health.
The supplied admin authorizer and probe callback must be trusted application
components; provider execution still goes through the normal adapter lifecycle.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .connection_manager import ConnectionManager, MODALITY_CONTRACTS
from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_VERSION_RE = re.compile(r"(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\Z")
_CAPABILITY_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_STATUSES = {"PENDING_APPROVAL", "APPROVED", "PROBE_FAILED", "PROBE_SUCCEEDED", "REJECTED", "ACTIVE"}


def _safe_https_url(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False, None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False, None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False, None
    host = parsed.hostname.casefold().rstrip(".") if parsed.hostname else None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not _HOST_RE.fullmatch(host)
    ):
        return False, None
    return True, host


class ProviderOnboardingManager:
    """Persist onboarding requests and activate only after approval and a real probe."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        connection_manager: ConnectionManager,
        *,
        authorize_admin: Callable[[str], Any],
        probe_provider: Callable[[dict[str, Any]], dict[str, Any]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(connection_manager, ConnectionManager):
            raise ValueError("connection_manager must be a ConnectionManager")
        if not callable(authorize_admin) or not callable(probe_provider):
            raise ValueError("admin authorizer and provider probe callbacks are required")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.connection_manager = connection_manager
        self.authorize_admin = authorize_admin
        self.probe_provider = probe_provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "requests": {}}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "requests": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid provider onboarding state") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("requests"), dict)
            or len(loaded["requests"]) > 10000
        ):
            raise ValueError("invalid provider onboarding state schema")
        for provider_id, record in loaded["requests"].items():
            if (
                not isinstance(provider_id, str)
                or not _ID_RE.fullmatch(provider_id)
                or not isinstance(record, dict)
                or record.get("provider_id") != provider_id
                or record.get("status") not in _STATUSES
                or not isinstance(record.get("manifest"), dict)
                or self._validate_manifest(record["manifest"])[0] is not None
            ):
                raise ValueError("invalid provider onboarding record")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._load()
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

    @staticmethod
    def _validate_manifest(manifest: Any) -> tuple[str | None, dict[str, Any] | None]:
        if not isinstance(manifest, dict):
            return "INVALID_MANIFEST", None
        required = ("id", "name", "category", "auth_type", "connection_type")
        if any(not isinstance(manifest.get(key), str) or not manifest[key].strip() for key in required):
            return "REQUIRED_METADATA_MISSING", None
        provider_id = manifest["id"].strip()
        if not _ID_RE.fullmatch(provider_id):
            return "INVALID_PROVIDER_ID", None
        if manifest["category"] not in MODALITY_CONTRACTS:
            return "UNSUPPORTED_PROVIDER_CATEGORY", None
        if manifest["connection_type"].upper() not in {"API", "LOCAL"}:
            return "INVALID_CONNECTION_TYPE", None
        if manifest.get("enabled", False) is not False:
            return "ONBOARDING_MUST_START_DISABLED", None

        api_ok, api_host = _safe_https_url(manifest.get("official_api_url"))
        docs_ok, _ = _safe_https_url(manifest.get("official_docs_url"))
        if not api_ok or not docs_ok:
            return "OFFICIAL_HTTPS_URLS_REQUIRED", None
        allowed_hosts = manifest.get("allowed_hosts")
        if (
            not isinstance(allowed_hosts, list)
            or not 1 <= len(allowed_hosts) <= 32
            or any(not isinstance(host, str) or not _HOST_RE.fullmatch(host) for host in allowed_hosts)
            or len({host.casefold().rstrip(".") for host in allowed_hosts}) != len(allowed_hosts)
            or api_host not in {host.casefold().rstrip(".") for host in allowed_hosts}
        ):
            return "INVALID_API_HOST_ALLOWLIST", None

        capabilities = manifest.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not 1 <= len(capabilities) <= 100
            or any(not isinstance(item, str) or not _CAPABILITY_RE.fullmatch(item) for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            return "INVALID_CAPABILITIES", None
        adapter_version = manifest.get("adapter_api_version", "1.0")
        if not isinstance(adapter_version, str) or not _VERSION_RE.fullmatch(adapter_version):
            return "INVALID_ADAPTER_API_VERSION", None
        subscription_required = manifest.get("subscription_required", True)
        if not isinstance(subscription_required, bool):
            return "INVALID_SUBSCRIPTION_FLAG", None
        if ConnectionManager._contains_credential_value_field(manifest):
            return "SENSITIVE_METADATA_REJECTED", None

        safe = {
            "id": provider_id,
            "name": manifest["name"].strip()[:160],
            "category": manifest["category"],
            "auth_type": manifest["auth_type"].strip()[:40],
            "connection_type": manifest["connection_type"].upper(),
            "official_api_url": manifest["official_api_url"],
            "official_docs_url": manifest["official_docs_url"],
            "allowed_hosts": [host.casefold().rstrip(".") for host in allowed_hosts],
            "capabilities": list(capabilities),
            "adapter_api_version": adapter_version,
            "subscription_required": subscription_required,
            "enabled": False,
        }
        if manifest.get("subscription_url") is not None:
            ok, _ = _safe_https_url(manifest["subscription_url"])
            if not ok:
                return "INVALID_SUBSCRIPTION_URL", None
            safe["subscription_url"] = manifest["subscription_url"]
        return None, safe

    def _admin_allowed(self, principal_id: str | None) -> bool:
        if not isinstance(principal_id, str) or not principal_id.strip():
            return False
        try:
            decision = self.authorize_admin(principal_id)
        except Exception:
            return False
        return decision is True or (
            isinstance(decision, dict) and decision.get("allowed") is True
        )

    def submit(self, manifest: dict[str, Any]) -> dict[str, Any]:
        error, safe_manifest = self._validate_manifest(manifest)
        if error is not None or safe_manifest is None:
            return {"status": error or "INVALID_MANIFEST", "accepted": False}
        provider_id = safe_manifest["id"]
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            existing = self.data["requests"].get(provider_id)
            if isinstance(existing, dict):
                same = existing.get("manifest") == safe_manifest
                return {
                    "status": "ALREADY_SUBMITTED" if same else "REQUEST_CONFLICT",
                    "accepted": same,
                    "provider_id": provider_id,
                    "onboarding_status": existing.get("status"),
                }
            self.data["requests"][provider_id] = {
                "provider_id": provider_id,
                "manifest": safe_manifest,
                "status": "PENDING_APPROVAL",
                "created_at": now,
                "updated_at": now,
            }
            self._save()
        return {"status": "ONBOARDING_SUBMITTED", "accepted": True, "provider_id": provider_id}

    def approve(self, provider_id: str, *, principal_id: str) -> dict[str, Any]:
        if not self._admin_allowed(principal_id):
            return {"status": "ADMIN_APPROVAL_REQUIRED", "approved": False}
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_NOT_FOUND", "approved": False}
            if record.get("status") != "PENDING_APPROVAL":
                return {"status": "INVALID_ONBOARDING_TRANSITION", "approved": False}
            record["status"] = "APPROVED"
            record["approved_at"] = self.clock().astimezone(timezone.utc).isoformat()
            record["updated_at"] = record["approved_at"]
            self._save()
        return {"status": "ONBOARDING_APPROVED", "approved": True, "provider_id": provider_id}

    def reject(self, provider_id: str, *, principal_id: str, reason_code: str = "ADMIN_REJECTED") -> dict[str, Any]:
        if not self._admin_allowed(principal_id):
            return {"status": "ADMIN_APPROVAL_REQUIRED", "rejected": False}
        if not isinstance(reason_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason_code):
            return {"status": "INVALID_REJECTION_REASON", "rejected": False}
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_NOT_FOUND", "rejected": False}
            if record.get("status") not in {"PENDING_APPROVAL", "APPROVED", "PROBE_FAILED"}:
                return {"status": "INVALID_ONBOARDING_TRANSITION", "rejected": False}
            record["status"] = "REJECTED"
            record["rejection_reason_code"] = reason_code
            record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
        return {"status": "ONBOARDING_REJECTED", "rejected": True, "provider_id": provider_id}

    def verify(self, provider_id: str, *, principal_id: str) -> dict[str, Any]:
        if not self._admin_allowed(principal_id):
            return {"status": "ADMIN_PROBE_REQUIRED", "verified": False}
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_NOT_FOUND", "verified": False}
            if record.get("status") not in {"APPROVED", "PROBE_FAILED"}:
                return {"status": "ONBOARDING_NOT_APPROVED", "verified": False}
            manifest = copy.deepcopy(record["manifest"])

        try:
            probe_result = self.probe_provider(manifest)
        except Exception as exc:
            probe_result = {"verified": False, "status": "PROBE_EXCEPTION"}
            error_type = type(exc).__name__
        else:
            error_type = None
        verified = isinstance(probe_result, dict) and probe_result.get("verified") is True
        raw_status = probe_result.get("status") if isinstance(probe_result, dict) else None
        probe_status = raw_status if isinstance(raw_status, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", raw_status) else (
            "PROBE_VERIFIED" if verified else "PROBE_FAILED"
        )
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict) or record.get("status") not in {"APPROVED", "PROBE_FAILED"}:
                return {"status": "ONBOARDING_STATE_CHANGED", "verified": False}
            record["probe_status"] = probe_status
            record["probe_verified_at"] = self.clock().astimezone(timezone.utc).isoformat() if verified else None
            record["status"] = "PROBE_SUCCEEDED" if verified else "PROBE_FAILED"
            record["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            if error_type is not None:
                record["probe_error_type"] = error_type
            self._save()
        return {
            "status": "PROVIDER_PROBE_VERIFIED" if verified else "PROVIDER_PROBE_FAILED",
            "verified": verified,
            "provider_id": provider_id,
            "probe_status": probe_status,
        }

    def activate(self, provider_id: str, *, principal_id: str) -> dict[str, Any]:
        if not self._admin_allowed(principal_id):
            return {"status": "ADMIN_ACTIVATION_REQUIRED", "activated": False}
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_NOT_FOUND", "activated": False}
            if record.get("status") != "PROBE_SUCCEEDED":
                return {"status": "LIVE_PROBE_REQUIRED", "activated": False}
            result = self.connection_manager.register_provider(
                copy.deepcopy(record["manifest"]), overwrite=False
            )
            if not isinstance(result, dict) or result.get("saved") is not True:
                status = result.get("status", "CATALOG_REGISTRATION_FAILED") if isinstance(result, dict) else "INVALID_CATALOG_RESPONSE"
                return {"status": status, "activated": False}
            record["status"] = "ACTIVE"
            record["activated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            record["updated_at"] = record["activated_at"]
            self._save()
        return {"status": "PROVIDER_ONBOARDED_DISABLED", "activated": True, "provider_id": provider_id,
                "note": "Provider is registered but remains disabled; enablement is a separate authorized operation."}

    def get_status(self, provider_id: str) -> dict[str, Any]:
        with self._transaction():
            record = self.data["requests"].get(provider_id)
            if not isinstance(record, dict):
                return {"status": "ONBOARDING_NOT_FOUND", "provider_id": provider_id}
            return {
                "status": "ONBOARDING_STATUS",
                "provider_id": provider_id,
                "onboarding_status": record["status"],
                "probe_status": record.get("probe_status"),
                "created_at": record.get("created_at"),
                "updated_at": record.get("updated_at"),
            }
