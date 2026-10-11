"""Provider connection state management.

This module records provider state; it does not claim a live API connection
unless a separate provider-specific probe has actually succeeded.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG = MODULE_DIR / "provider_catalog.json"
MASTER_CATALOG = MODULE_DIR / "provider_catalog_master.json"
DEFAULT_STATE = MODULE_DIR / "connection_state.json"


class ConnectionManager:
    VALID_STATES = {
        "LOCKED",
        "CONNECTING",
        "CONNECTED",
        "READY",
        "CREDENTIALS_PRESENT",
        "CREDENTIALS_MISSING",
        "CREDITS_UNAVAILABLE",
        "DISABLED",
        "ERROR",
        "UNKNOWN",
    }

    def __init__(
        self,
        catalog_path: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path) if catalog_path else self._default_catalog()
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE
        self.catalog = self._load_json(self.catalog_path)
        self.state = self._load_json(self.state_path, default={"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}})
        self.state.setdefault("providers", {})
        # Runtime instances are intentionally process-local and never serialized.
        self._loaded_providers: dict[str, Any] = {}

    @staticmethod
    def _default_catalog() -> Path:
        if DEFAULT_CATALOG.is_file():
            return DEFAULT_CATALOG
        if MASTER_CATALOG.is_file():
            return MASTER_CATALOG
        return DEFAULT_CATALOG

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.exists():
            if default is not None:
                return json.loads(json.dumps(default))
            raise FileNotFoundError(f"Required provider catalog not found: {path}")
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path.name}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in {path.name}")
        return data

    @staticmethod
    def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
        """Persist JSON with same-directory atomic replacement."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _save(self) -> None:
        self._atomic_write_json(self.state_path, self.state)

    def list_providers(self) -> list[dict[str, Any]]:
        providers = self.catalog.get("providers", [])
        if not isinstance(providers, list):
            raise ValueError("Provider catalog 'providers' must be a list")
        return [item for item in providers if isinstance(item, dict)]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return next((p for p in self.list_providers() if p.get("id") == provider_id), None)

    def provider_exists(self, provider_id: str) -> bool:
        return self.get_provider(provider_id) is not None

    def load_provider(self, provider_id: str, factory: Any) -> dict[str, Any]:
        """Instantiate a provider through an injected factory, without dynamic imports."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "loaded": False}
        if provider.get("enabled") is False:
            return {"status": "DISABLED", "provider": provider_id, "loaded": False}
        if provider_id in self._loaded_providers:
            return {"status": "ALREADY_LOADED", "provider": provider_id, "loaded": True}
        if not callable(factory):
            return {"status": "INVALID_FACTORY", "provider": provider_id, "loaded": False}
        try:
            instance = factory(provider)
        except Exception as exc:
            # Never return raw exception text: it may contain endpoints or credentials.
            return {
                "status": "LOAD_FAILED",
                "provider": provider_id,
                "error_type": type(exc).__name__,
                "loaded": False,
            }
        if instance is None:
            return {"status": "LOAD_FAILED", "provider": provider_id, "loaded": False}
        self._loaded_providers[provider_id] = instance
        return {"status": "LOADED", "provider": provider_id, "loaded": True}

    def get_loaded_provider(self, provider_id: str) -> Any | None:
        """Return a process-local instance; callers must not persist or log it."""
        return self._loaded_providers.get(provider_id)

    def unload_provider(self, provider_id: str) -> dict[str, Any]:
        """Release a loaded provider; preserve it if its cleanup hook fails."""
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id, "unloaded": False}
        if provider_id not in self._loaded_providers:
            return {"status": "NOT_LOADED", "provider": provider_id, "unloaded": False}
        instance = self._loaded_providers[provider_id]
        cleanup = getattr(instance, "close", None)
        if not callable(cleanup):
            cleanup = getattr(instance, "shutdown", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception as exc:
                # Keep the instance registered so the caller can inspect/retry cleanup.
                return {
                    "status": "UNLOAD_FAILED",
                    "provider": provider_id,
                    "error_type": type(exc).__name__,
                    "unloaded": False,
                }
        del self._loaded_providers[provider_id]
        return {"status": "UNLOADED", "provider": provider_id, "unloaded": True}

    def discover_providers(
        self,
        *,
        category: str | None = None,
        capability: str | None = None,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Discover catalog entries by modality/capability without probing providers."""
        wanted_category = category.strip().casefold() if category else None
        wanted_capability = capability.strip().casefold() if capability else None
        discovered: list[dict[str, Any]] = []
        for provider in self.list_providers():
            if wanted_category and str(provider.get("category", "")).casefold() != wanted_category:
                continue
            if enabled_only and provider.get("enabled") is not True:
                continue
            if wanted_capability:
                raw_capabilities = provider.get("capabilities", [])
                if isinstance(raw_capabilities, str):
                    capabilities = [raw_capabilities]
                elif isinstance(raw_capabilities, list):
                    capabilities = [str(item) for item in raw_capabilities]
                else:
                    capabilities = []
                # Category is also a discoverable modality for simple legacy catalog entries.
                supported = {item.casefold() for item in capabilities}
                if provider.get("category"):
                    supported.add(str(provider["category"]).casefold())
                if wanted_capability not in supported:
                    continue
            discovered.append(dict(provider))
        return discovered

    def register_provider(
        self, provider: dict[str, Any], *, overwrite: bool = False
    ) -> dict[str, Any]:
        """Register validated provider metadata; never accept credentials in catalog data."""
        if not isinstance(provider, dict):
            return {"status": "INVALID_PROVIDER", "reason": "provider must be an object"}
        required = ("id", "name", "category", "auth_type", "connection_type")
        if any(not isinstance(provider.get(key), str) or not provider[key].strip() for key in required):
            return {
                "status": "INVALID_PROVIDER",
                "reason": "required non-empty string fields: " + ", ".join(required),
            }
        provider_id = provider["id"].strip()
        if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in provider_id):
            return {"status": "INVALID_PROVIDER", "reason": "provider id contains unsupported characters"}

        # Metadata may name an environment variable, but must never contain a secret value.
        forbidden = {"api_key", "apikey", "token", "secret", "password", "credential_value"}
        if any(str(key).lower().replace("-", "_") in forbidden for key in provider):
            return {"status": "SENSITIVE_METADATA_REJECTED", "provider": provider_id}

        providers = self.list_providers()
        existing = next((item for item in providers if item.get("id") == provider_id), None)
        if existing is not None and not overwrite:
            return {"status": "ALREADY_EXISTS", "provider": provider_id, "saved": False}
        safe_provider = dict(provider)
        safe_provider["id"] = provider_id
        updated = [item for item in providers if item.get("id") != provider_id]
        updated.append(safe_provider)
        new_catalog = dict(self.catalog)
        new_catalog["providers"] = updated
        self._atomic_write_json(self.catalog_path, new_catalog)
        self.catalog = new_catalog
        return {
            "status": "REGISTERED",
            "provider": provider_id,
            "saved": True,
            "overwritten": existing is not None,
        }

    def get_state(self, provider_id: str) -> dict[str, Any]:
        item = self.state["providers"].get(provider_id)
        if not isinstance(item, dict):
            return {"status": "UNKNOWN", "verified": False}
        result = dict(item)
        # Older records may say CONNECTED without any live verification evidence.
        if result.get("status") == "CONNECTED" and result.get("verified") is not True:
            result["reported_status"] = "CONNECTED"
            result["status"] = "UNKNOWN"
            result["verified"] = False
        # Return a copy so callers cannot mutate internal state without validation.
        return result

    def set_state(
        self,
        provider_id: str,
        status: str,
        *,
        verified: bool = False,
    ) -> dict[str, Any]:
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id}
        if status not in self.VALID_STATES:
            return {"status": "INVALID_STATE", "provider": provider_id}
        if status == "CONNECTED" and not verified:
            return {
                "status": "VERIFICATION_REQUIRED",
                "provider": provider_id,
                "saved": False,
            }

        record: dict[str, Any] = {
            "status": status,
            "verified": bool(verified),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state["providers"][provider_id] = record
        self._save()
        return {"provider": provider_id, **record, "saved": True}

    def credential_status(self, provider_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}

        auth_type = str(provider.get("auth_type", "NONE")).upper()
        token_env = provider.get("token_env")
        if not token_env:
            token_env = {
                "huggingface": "HF_TOKEN",
                "openai_image": "OPENAI_API_KEY",
                "openai_voice": "OPENAI_API_KEY",
                "elevenlabs": "ELEVENLABS_API_KEY",
                "fal_ai": "FAL_KEY",
            }.get(provider_id)

        if auth_type in {"NONE", "PUBLIC", "ANONYMOUS"}:
            return {"provider": provider_id, "status": "NO_CREDENTIAL_REQUIRED", "credential_present": True}
        if not token_env:
            return {"provider": provider_id, "status": "MANUAL_CONNECTION_REQUIRED", "credential_present": False}

        env = os.environ if environ is None else environ
        present = bool(str(env.get(str(token_env), "")).strip())
        return {
            "provider": provider_id,
            "status": "CREDENTIALS_PRESENT" if present else "CREDENTIALS_MISSING",
            "credential_present": present,
            "credential_name": token_env,
        }

    def summary(self) -> dict[str, int]:
        result = {state: 0 for state in sorted(self.VALID_STATES)}
        for item in self.state["providers"].values():
            status = item.get("status", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
            # Do not count legacy, unverified CONNECTED records as healthy connections.
            if status == "CONNECTED" and item.get("verified") is not True:
                status = "UNKNOWN"
            if status not in result:
                status = "UNKNOWN"
            result[status] += 1
        return result


if __name__ == "__main__":
    manager = ConnectionManager()
    print("===================================")
    print("       PROVIDER CONNECTION MANAGER")
    print("===================================")
    print("Catalog providers:", len(manager.list_providers()))
    print("Tracked connections:", len(manager.state["providers"]))
    print("Summary:", manager.summary())
    print("Credential checks report presence only; they do not prove live API access.")
