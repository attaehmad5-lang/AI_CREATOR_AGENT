"""Safe registration boundary for self-hosted/local model endpoints.

Local endpoints are disabled by default and are never treated as verified merely because
they are reachable. Remote private/self-hosted endpoints require explicit host allowlisting
and HTTPS. Runtime adapter execution remains an application integration.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


class LocalProviderManager:
    CAPABILITIES = frozenset({
        "text_generation", "image_generation", "image_editing", "embedding",
        "audio_generation", "transcription", "video_generation",
    })

    def __init__(self, path: str | os.PathLike[str], *, max_providers: int = 500) -> None:
        if not isinstance(max_providers, int) or isinstance(max_providers, bool) or not 1 <= max_providers <= 10000:
            raise ValueError("max_providers is outside supported limits")
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.max_providers = max_providers
        self._thread_lock = threading.RLock()
        self._ensure_file()

    @staticmethod
    def _valid_provider_id(value: Any) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", value))

    @staticmethod
    def _is_loopback(host: str) -> bool:
        if host.casefold() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @classmethod
    def validate_endpoint(cls, endpoint: Any, *, allowed_hosts: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
            return {"valid": False, "status": "INVALID_LOCAL_ENDPOINT"}
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            return {"valid": False, "status": "INVALID_LOCAL_ENDPOINT"}
        if (
            parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(ord(ch) < 32 for ch in endpoint)
        ):
            return {"valid": False, "status": "INVALID_LOCAL_ENDPOINT"}
        host = host.rstrip(".").casefold()
        if port is not None and not 1 <= port <= 65535:
            return {"valid": False, "status": "INVALID_LOCAL_ENDPOINT"}
        allowed = {
            item.rstrip(".").casefold() for item in (allowed_hosts or [])
            if isinstance(item, str) and item.strip()
        }
        if cls._is_loopback(host):
            if parsed.scheme != "http" and parsed.scheme != "https":
                return {"valid": False, "status": "INVALID_LOCAL_ENDPOINT"}
            return {"valid": True, "status": "LOCAL_ENDPOINT_VALID", "host": host, "is_loopback": True}
        if host not in allowed:
            return {"valid": False, "status": "LOCAL_HOST_NOT_ALLOWLISTED"}
        if parsed.scheme != "https":
            return {"valid": False, "status": "REMOTE_LOCAL_ENDPOINT_REQUIRES_HTTPS"}
        return {"valid": True, "status": "LOCAL_ENDPOINT_VALID", "host": host, "is_loopback": False}

    def _empty(self) -> dict[str, Any]:
        return {"schema_version": 1, "providers": {}}

    def _validate_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("providers"), dict):
            raise ValueError("local provider registry is invalid")
        providers = state["providers"]
        if len(providers) > self.max_providers:
            raise ValueError("local provider registry exceeds capacity")
        for provider_id, record in providers.items():
            if not self._valid_provider_id(provider_id) or not isinstance(record, dict):
                raise ValueError("local provider record is invalid")
            if (
                not isinstance(record.get("endpoint"), str)
                or not isinstance(record.get("model"), str)
                or not isinstance(record.get("capabilities"), list)
                or not isinstance(record.get("enabled"), bool)
                or not isinstance(record.get("verified"), bool)
            ):
                raise ValueError("local provider fields are invalid")
            validation = self.validate_endpoint(record["endpoint"], allowed_hosts=record.get("allowed_hosts"))
            if not validation["valid"] or not record["model"].strip() or len(record["model"]) > 200:
                raise ValueError("local provider configuration is invalid")
            if not record["capabilities"] or any(cap not in self.CAPABILITIES for cap in record["capabilities"]):
                raise ValueError("local provider capabilities are invalid")
            if record["enabled"] and not record["verified"]:
                raise ValueError("unverified local provider cannot be enabled")
        return state

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = self._empty()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("local provider registry cannot be read") from None
        return self._validate_state(state)

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _ensure_file(self) -> None:
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            if not self.path.exists():
                self._save_unlocked(self._empty())
            else:
                self._load_unlocked()

    def register(
        self,
        *,
        provider_id: str,
        endpoint: str,
        model: str,
        capabilities: list[str],
        allowed_hosts: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._valid_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(
            marker in model.casefold() for marker in ("token=", "api_key=", "password=", "secret=", "bearer ")
        ):
            return {"status": "INVALID_MODEL_ID", "registered": False}
        if (
            not isinstance(capabilities, list) or not capabilities
            or len(capabilities) != len(set(capabilities))
            or any(not isinstance(cap, str) or cap not in self.CAPABILITIES for cap in capabilities)
        ):
            return {"status": "INVALID_CAPABILITIES", "registered": False}
        if allowed_hosts is not None and (
            not isinstance(allowed_hosts, list)
            or any(not isinstance(host, str) or not host.strip() or len(host) > 253 for host in allowed_hosts)
        ):
            return {"status": "INVALID_HOST_ALLOWLIST", "registered": False}
        validation = self.validate_endpoint(endpoint, allowed_hosts=allowed_hosts)
        if not validation["valid"]:
            return {"status": validation["status"], "registered": False}
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            if provider_id in state["providers"]:
                return {"status": "PROVIDER_ALREADY_REGISTERED", "registered": False}
            if len(state["providers"]) >= self.max_providers:
                return {"status": "LOCAL_PROVIDER_CAPACITY_REACHED", "registered": False}
            state["providers"][provider_id] = {
                "endpoint": endpoint, "model": model.strip(),
                "capabilities": sorted(capabilities),
                "allowed_hosts": sorted({h.rstrip(".").casefold() for h in (allowed_hosts or [])}),
                "enabled": False, "verified": False,
            }
            self._save_unlocked(state)
        return {"status": "REGISTERED_DISABLED", "registered": True, "enabled": False}

    def enable(self, provider_id: str, *, probe: Callable[..., Any], timeout_seconds: float = 5.0) -> dict[str, Any]:
        if not self._valid_provider_id(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "enabled": False}
        if not callable(probe):
            return {"status": "LIVE_PROBE_REQUIRED", "enabled": False}
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 60:
            return {"status": "INVALID_TIMEOUT", "enabled": False}
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            record = state["providers"].get(provider_id)
            if record is None:
                return {"status": "PROVIDER_NOT_FOUND", "enabled": False}
            snapshot = dict(record)
        try:
            result = probe(endpoint=snapshot["endpoint"], model=snapshot["model"], timeout_seconds=float(timeout_seconds))
        except Exception:
            return {"status": "LOCAL_PROVIDER_PROBE_FAILED", "enabled": False}
        if not isinstance(result, dict) or result.get("ok") is not True or result.get("model") != snapshot["model"]:
            return {"status": "LOCAL_PROVIDER_MODEL_UNVERIFIED", "enabled": False}
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            record = state["providers"].get(provider_id)
            if record is None or record != snapshot:
                return {"status": "LOCAL_PROVIDER_CHANGED_DURING_PROBE", "enabled": False}
            record["verified"] = True
            record["enabled"] = True
            self._save_unlocked(state)
        return {"status": "LOCAL_PROVIDER_ENABLED", "enabled": True, "verified": True}

    def disable(self, provider_id: str) -> dict[str, Any]:
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            record = state["providers"].get(provider_id)
            if record is None:
                return {"status": "PROVIDER_NOT_FOUND", "disabled": False}
            record["enabled"] = False
            record["verified"] = False
            self._save_unlocked(state)
        return {"status": "LOCAL_PROVIDER_DISABLED", "disabled": True}

    def list_providers(self) -> list[dict[str, Any]]:
        with self._thread_lock, interprocess_file_lock(self.lock_path):
            state = self._load_unlocked()
            return [
                {"id": provider_id, **record}
                for provider_id, record in sorted(state["providers"].items())
            ]
