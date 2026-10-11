"""Provider connection state management.

This module records provider state; it does not claim a live API connection
unless a separate provider-specific probe has actually succeeded.
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import interprocess_file_lock


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
        self.validate_catalog_schema(self.catalog, raise_on_error=True)
        self.state = self._load_json(self.state_path, default={"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}})
        if not isinstance(self.state.get("providers"), dict):
            raise ValueError("Invalid connection state schema: providers must be an object")
        self._lock = threading.RLock()
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

    def _reload_catalog(self) -> None:
        catalog = self._load_json(self.catalog_path)
        self.validate_catalog_schema(catalog, raise_on_error=True)
        self.catalog = catalog

    def _reload_state(self) -> None:
        state = self._load_json(
            self.state_path,
            default={"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}},
        )
        if not isinstance(state.get("providers"), dict):
            raise ValueError("Invalid connection state schema: providers must be an object")
        self.state = state

    @contextmanager
    def _catalog_transaction(self) -> Iterator[None]:
        """Lock, reload, mutate, and persist the shared provider catalog atomically."""
        with self._lock:
            lock_path = self.catalog_path.with_name(self.catalog_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_catalog()
                yield

    @contextmanager
    def _state_transaction(self) -> Iterator[None]:
        """Lock and reload shared connection state before each read-modify-write."""
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload_state()
                yield

    @staticmethod
    def _contains_credential_value_field(value: Any) -> bool:
        """Detect secret-bearing keys recursively while allowing environment references."""
        safe_reference_keys = {"token_env", "token_expiry_env", "api_key_env", "secret_ref", "credential_env"}
        forbidden_keys = {
            "api_key", "apikey", "api_token", "token", "access_token",
            "refresh_token", "password", "secret", "credential", "credential_value",
            "private_key", "authorization", "bearer_token",
        }
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in safe_reference_keys:
                    continue
                if normalized in forbidden_keys or any(
                    marker in normalized
                    for marker in (
                        "access_token", "refresh_token", "api_key", "apikey",
                        "password", "secret", "credential", "authorization",
                        "bearer", "token", "private_key", "signing_key",
                        "encryption_key", "key_material", "cookie", "session",
                    )
                ):
                    return True
                if ConnectionManager._contains_credential_value_field(item):
                    return True
        elif isinstance(value, list):
            return any(ConnectionManager._contains_credential_value_field(item) for item in value)
        return False

    @staticmethod
    def validate_catalog_schema(
        catalog: dict[str, Any], *, raise_on_error: bool = False
    ) -> dict[str, Any]:
        """Validate catalog structure, provider identities, and safe metadata fields."""
        errors: list[str] = []
        if not isinstance(catalog, dict):
            errors.append("catalog must be an object")
        else:
            providers = catalog.get("providers")
            if not isinstance(providers, list):
                errors.append("providers must be a list")
            else:
                seen: set[str] = set()
                for index, provider in enumerate(providers):
                    if not isinstance(provider, dict):
                        errors.append(f"providers[{index}] must be an object")
                        continue
                    provider_id = provider.get("id")
                    if not isinstance(provider_id, str) or not provider_id.strip():
                        errors.append(f"providers[{index}].id must be a non-empty string")
                    elif provider_id in seen:
                        errors.append(f"duplicate provider id: {provider_id}")
                    else:
                        seen.add(provider_id)
                    for key in ("name", "category", "auth_type", "connection_type"):
                        if key in provider and not isinstance(provider[key], str):
                            errors.append(f"providers[{index}].{key} must be a string")
                    if "enabled" in provider and not isinstance(provider["enabled"], bool):
                        errors.append(f"providers[{index}].enabled must be boolean")
                    for reference_key in ("token_env", "token_expiry_env", "api_key_env", "credential_env"):
                        if reference_key in provider and (
                            not isinstance(provider[reference_key], str)
                            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", provider[reference_key])
                        ):
                            errors.append(
                                f"providers[{index}].{reference_key} must be a valid environment-variable name"
                            )
                    if "secret_ref" in provider and (
                        not isinstance(provider["secret_ref"], str)
                        or not provider["secret_ref"].strip()
                        or len(provider["secret_ref"]) > 256
                        or any(ord(char) < 32 for char in provider["secret_ref"])
                    ):
                        errors.append(f"providers[{index}].secret_ref must be a safe non-empty reference")
                    if "capabilities" in provider and not (
                        isinstance(provider["capabilities"], list)
                        and all(isinstance(item, str) for item in provider["capabilities"])
                    ):
                        errors.append(f"providers[{index}].capabilities must be a list of strings")
                    if ConnectionManager._contains_credential_value_field(provider):
                        errors.append(f"providers[{index}] contains a forbidden credential value field")
        result = {"valid": not errors, "errors": errors, "provider_count": len(catalog.get("providers", [])) if isinstance(catalog, dict) and isinstance(catalog.get("providers"), list) else 0}
        if errors and raise_on_error:
            raise ValueError("Invalid provider catalog: " + "; ".join(errors))
        return result

    def _list_providers_unlocked(self) -> list[dict[str, Any]]:
        providers = self.catalog.get("providers", [])
        if not isinstance(providers, list):
            raise ValueError("Provider catalog 'providers' must be a list")
        return [item for item in providers if isinstance(item, dict)]

    @staticmethod
    def _public_provider_view(provider: dict[str, Any]) -> dict[str, Any]:
        """Detach catalog data and distinguish catalog labels from live connection state."""
        import copy
        safe = copy.deepcopy(provider)
        if "status" in safe:
            safe["catalog_status"] = safe.pop("status")
        return safe

    def list_providers(self) -> list[dict[str, Any]]:
        """Return detached provider records; catalog status is never live-connection proof."""
        with self._catalog_transaction():
            return [self._public_provider_view(item) for item in self._list_providers_unlocked()]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return next((p for p in self.list_providers() if p.get("id") == provider_id), None)

    def provider_exists(self, provider_id: str) -> bool:
        return self.get_provider(provider_id) is not None

    def get_provider_metadata(self, provider_id: str) -> dict[str, Any] | None:
        """Return a detached, secret-filtered view of catalog metadata."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return None
        safe: dict[str, Any] = {}
        for key, value in provider.items():
            normalized = str(key).casefold().replace("-", "_")
            # Environment-variable/reference names are metadata, not credential values.
            if normalized in {"token_env", "token_expiry_env", "api_key_env", "secret_ref", "credential_env"}:
                safe[key] = value
                continue
            if any(term in normalized for term in ("password", "secret", "credential", "api_key", "apikey", "access_token", "refresh_token")):
                continue
            if normalized == "token":
                continue
            safe[key] = value
        return safe

    def set_provider_enabled(
        self, provider_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable/disable catalog eligibility only; never purchases or proves connectivity."""
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_VALUE", "provider": provider_id, "saved": False}
        with self._catalog_transaction():
            providers = self._list_providers_unlocked()
            index = next((i for i, item in enumerate(providers) if item.get("id") == provider_id), None)
            if index is None:
                return {"status": "NOT_FOUND", "provider": provider_id, "saved": False}
            updated = [dict(item) for item in providers]
            updated[index]["enabled"] = enabled
            new_catalog = dict(self.catalog)
            new_catalog["providers"] = updated
            self._atomic_write_json(self.catalog_path, new_catalog)
            self.catalog = new_catalog
        if not enabled and provider_id in self._loaded_providers:
            unload_result = self.unload_provider(provider_id)
            if unload_result["status"] not in {"UNLOADED", "NOT_LOADED"}:
                # The catalog remains disabled even if cleanup failed: fail closed.
                return {
                    "status": "DISABLED_CLEANUP_FAILED",
                    "provider": provider_id,
                    "saved": True,
                    "runtime_unloaded": False,
                }
        return {
            "status": "ENABLED" if enabled else "DISABLED",
            "provider": provider_id,
            "saved": True,
            "runtime_unloaded": not enabled,
            "note": "Eligibility only; no purchase, billing, or live connectivity check performed.",
        }

    def is_provider_enabled(self, provider_id: str) -> bool:
        provider = self.get_provider(provider_id)
        return provider is not None and provider.get("enabled") is True

    def validate_provider_contract(self, provider_id: str, instance: Any) -> dict[str, Any]:
        """Validate the callable surface required by the provider's declared modality."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "valid": False}
        category = str(provider.get("category", "")).casefold().replace("-", "_").replace(" ", "_")
        contracts: dict[str, tuple[tuple[str, ...], ...]] = {
            "image": (("generate_image",),),
            "video": (("generate_video",),),
            "voice": (("generate_speech", "generate_audio"),),
            "audio": (("generate_audio", "generate_speech"),),
            "music": (("generate_music",),),
            "text": (("generate_text",),),
            "language_model": (("generate_text",),),
            "transcription": (("transcribe",),),
            "translation": (("translate",),),
            "dubbing": (("dub",),),
            "image_editing": (("edit_image",),),
            "upscaling": (("upscale_image",),),
            "lipsync": (("generate_lipsync",),),
        }
        requirements = contracts.get(category)
        if requirements is None:
            return {
                "status": "UNSUPPORTED_MODALITY",
                "provider": provider_id,
                "valid": False,
                "contract_version": "1.0",
            }
        missing = [
            " or ".join(alternatives)
            for alternatives in requirements
            if not any(callable(getattr(instance, name, None)) for name in alternatives)
        ]
        return {
            "status": "VALID" if not missing else "CONTRACT_VIOLATION",
            "provider": provider_id,
            "valid": not missing,
            "missing_methods": missing,
            "contract_version": "1.0",
        }

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
        contract = self.validate_provider_contract(provider_id, instance)
        if not contract["valid"]:
            return {
                "status": "CONTRACT_VIOLATION",
                "provider": provider_id,
                "loaded": False,
                "contract_status": contract["status"],
                "missing_methods": contract.get("missing_methods", []),
            }
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
        if category is not None and (not isinstance(category, str) or not category.strip()):
            return []
        if capability is not None and (not isinstance(capability, str) or not capability.strip()):
            return []
        if not isinstance(enabled_only, bool):
            return []
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
        if self._contains_credential_value_field(provider):
            return {"status": "SENSITIVE_METADATA_REJECTED", "provider": provider_id}

        with self._catalog_transaction():
            providers = self._list_providers_unlocked()
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

    @staticmethod
    def _sanitize_public_data(value: Any) -> Any:
        """Recursively remove credential-like values from data returned to callers."""
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            safe_reference_keys = {"token_env", "token_expiry_env", "api_key_env", "secret_ref", "credential_env"}
            secret_fragments = (
                "password", "secret", "credential", "api_key", "apikey",
                "access_token", "refresh_token", "token", "authorization",
                "bearer", "private_key", "signing_key", "encryption_key",
                "key_material", "cookie", "session",
            )
            for key, item in value.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in safe_reference_keys:
                    safe[key] = item
                elif normalized == "token" or any(part in normalized for part in secret_fragments):
                    continue
                else:
                    safe[key] = ConnectionManager._sanitize_public_data(item)
            return safe
        if isinstance(value, list):
            return [ConnectionManager._sanitize_public_data(item) for item in value]
        if isinstance(value, tuple):
            return [ConnectionManager._sanitize_public_data(item) for item in value]
        return value

    def get_state(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "UNKNOWN", "verified": False}
        with self._state_transaction():
            item = self.state["providers"].get(provider_id)
            if not isinstance(item, dict):
                return {"status": "UNKNOWN", "verified": False}
            result = self._sanitize_public_data(item)
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
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "saved": False}
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not isinstance(status, str) or status not in self.VALID_STATES:
            return {"status": "INVALID_STATE", "provider": provider_id}
        if not isinstance(verified, bool):
            return {"status": "INVALID_VERIFICATION_FLAG", "provider": provider_id, "saved": False}
        # Public state updates may record administrative states, but cannot mint
        # proof of connectivity. Only verify_connectivity may mark a provider verified.
        if verified is True or status == "CONNECTED":
            return {
                "status": "VERIFICATION_REQUIRED",
                "provider": provider_id,
                "saved": False,
            }

        with self._state_transaction():
            existing = self.state["providers"].get(provider_id, {})
            record: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
            record.update({
                "status": status,
                "verified": bool(verified),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self.state["providers"][provider_id] = record
            self._save()
            return {"provider": provider_id, **self._sanitize_public_data(record), "saved": True}

    def _mark_connected_after_probe(self, provider_id: str) -> dict[str, Any]:
        """Record CONNECTED only after the provider-specific probe succeeds."""
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id, "saved": False}
        with self._state_transaction():
            existing = self.state["providers"].get(provider_id, {})
            record: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
            record.update({
                "status": "CONNECTED",
                "verified": True,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self.state["providers"][provider_id] = record
            self._save()
            return {"provider": provider_id, **self._sanitize_public_data(record), "saved": True}

    @staticmethod
    def validate_endpoint(
        endpoint: str,
        *,
        allowed_hosts: set[str] | None = None,
        allow_loopback_http: bool = False,
    ) -> dict[str, Any]:
        """Validate provider endpoint transport and host policy before any network request."""
        from ipaddress import ip_address
        from urllib.parse import urlsplit

        if not isinstance(allow_loopback_http, bool):
            return {"valid": False, "status": "INVALID_LOOPBACK_HTTP_POLICY"}
        if not isinstance(endpoint, str) or not endpoint.strip():
            return {"valid": False, "status": "INVALID_ENDPOINT"}
        if allowed_hosts is not None and (
            not isinstance(allowed_hosts, (set, frozenset, list, tuple))
            or any(not isinstance(host, str) or not host.strip() for host in allowed_hosts)
        ):
            return {"valid": False, "status": "INVALID_HOST_ALLOWLIST"}
        try:
            parsed = urlsplit(endpoint.strip())
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return {"valid": False, "status": "INVALID_ENDPOINT"}
        if parsed.scheme not in {"https", "http"} or not hostname:
            return {"valid": False, "status": "HTTPS_REQUIRED"}
        if parsed.username is not None or parsed.password is not None:
            return {"valid": False, "status": "EMBEDDED_CREDENTIALS_REJECTED"}
        if parsed.query:
            return {"valid": False, "status": "QUERY_PARAMETERS_REJECTED"}
        if parsed.fragment:
            return {"valid": False, "status": "FRAGMENT_REJECTED"}
        normalized_host = hostname.casefold().rstrip(".")
        normalized_allowed = (
            {str(host).casefold().rstrip(".") for host in allowed_hosts}
            if allowed_hosts is not None else None
        )
        is_loopback = normalized_host == "localhost"
        address = None
        try:
            address = ip_address(normalized_host)
            is_loopback = is_loopback or address.is_loopback
        except ValueError:
            pass
        if parsed.scheme == "http" and not (allow_loopback_http and is_loopback):
            return {"valid": False, "status": "HTTPS_REQUIRED"}
        # Private, link-local, and reserved literal IPs are unsafe defaults for
        # outbound provider calls. An explicit per-provider allowlist is required.
        if address is not None and not address.is_global and not (
            allow_loopback_http and is_loopback
        ) and (normalized_allowed is None or normalized_host not in normalized_allowed):
            return {"valid": False, "status": "NON_PUBLIC_ADDRESS_REJECTED"}
        if normalized_allowed is not None and normalized_host not in normalized_allowed:
            return {"valid": False, "status": "HOST_NOT_ALLOWLISTED"}
        return {
            "valid": True,
            "status": "VALID",
            "scheme": parsed.scheme,
            "hostname": normalized_host,
            "port": port,
            "tls_verification_required": parsed.scheme == "https",
        }

    def configure_provider(
        self,
        provider_id: str,
        *,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """Persist non-secret endpoint/model settings after endpoint safety checks."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "saved": False}
        configuration: dict[str, Any] = {}
        if model is not None:
            if not isinstance(model, str) or not model.strip() or len(model) > 200:
                return {"status": "INVALID_MODEL", "provider": provider_id, "saved": False}
            if any(term in model.casefold() for term in ("api_key=", "token=", "password=", "secret=")):
                return {"status": "SENSITIVE_CONFIGURATION_REJECTED", "provider": provider_id, "saved": False}
            configuration["model"] = model.strip()
        if endpoint is not None:
            allowed_hosts_raw = provider.get("allowed_hosts")
            allowed_hosts = (
                {item for item in allowed_hosts_raw if isinstance(item, str) and item.strip()}
                if isinstance(allowed_hosts_raw, list) else None
            )
            validation = self.validate_endpoint(endpoint, allowed_hosts=allowed_hosts)
            if not validation["valid"]:
                return {
                    "status": validation["status"],
                    "provider": provider_id,
                    "saved": False,
                }
            # Do not persist URLs containing query parameters: they can carry credentials.
            from urllib.parse import urlsplit
            if urlsplit(endpoint).query:
                return {"status": "QUERY_PARAMETERS_REJECTED", "provider": provider_id, "saved": False}
            if not allowed_hosts:
                return {
                    "status": "HOST_ALLOWLIST_REQUIRED",
                    "provider": provider_id,
                    "saved": False,
                }
            configuration["endpoint"] = endpoint.strip()
        if not configuration:
            return {"status": "NO_CONFIGURATION", "provider": provider_id, "saved": False}
        with self._state_transaction():
            state = self.state["providers"].get(provider_id, {})
            record = dict(state) if isinstance(state, dict) else {}
            current = record.get("configuration", {})
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(configuration)
            record["configuration"] = merged
            record["configuration_updated_at"] = datetime.now(timezone.utc).isoformat()
            self.state["providers"][provider_id] = record
            self._save()
            return {
                "status": "CONFIGURED",
                "provider": provider_id,
                "saved": True,
                "configuration": dict(merged),
            }

    def get_provider_configuration(self, provider_id: str) -> dict[str, Any]:
        """Return validated non-secret runtime configuration for a known provider."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "configuration": {}}
        with self._state_transaction():
            state = self.state["providers"].get(provider_id, {})
            raw = state.get("configuration", {}) if isinstance(state, dict) else {}
            if not isinstance(raw, dict):
                return {"status": "INVALID_CONFIGURATION", "provider": provider_id, "configuration": {}}
            configuration = self._sanitize_public_data(raw)
        if self._contains_credential_value_field(configuration):
            return {"status": "SENSITIVE_CONFIGURATION_REJECTED", "provider": provider_id, "configuration": {}}
        model = configuration.get("model")
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 200
        ):
            return {"status": "INVALID_MODEL", "provider": provider_id, "configuration": {}}
        endpoint = configuration.get("endpoint")
        if endpoint is not None:
            allowed_hosts_raw = provider.get("allowed_hosts")
            allowed_hosts = (
                {item for item in allowed_hosts_raw if isinstance(item, str) and item.strip()}
                if isinstance(allowed_hosts_raw, list) else None
            )
            if not allowed_hosts:
                return {"status": "HOST_ALLOWLIST_REQUIRED", "provider": provider_id, "configuration": {}}
            validation = self.validate_endpoint(endpoint, allowed_hosts=allowed_hosts)
            if not validation["valid"]:
                return {"status": validation["status"], "provider": provider_id, "configuration": {}}
        return {
            "status": "CONFIGURED" if configuration else "NO_CONFIGURATION",
            "provider": provider_id,
            "configuration": configuration,
        }

    def verify_connectivity(
        self,
        provider_id: str,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
        environ: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run an explicitly injected, timeout-aware probe; only success marks CONNECTED."""
        import time

        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "verified": False}
        if provider.get("enabled") is False:
            return {"status": "DISABLED", "provider": provider_id, "verified": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "provider": provider_id, "verified": False}
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "verified": False}
        configuration_result = self.get_provider_configuration(provider_id)
        if configuration_result.get("status") not in {"CONFIGURED", "NO_CONFIGURATION"}:
            try:
                self.set_state(provider_id, "ERROR")
            except Exception:
                pass
            return {
                "status": configuration_result.get("status", "INVALID_CONFIGURATION"),
                "provider": provider_id,
                "verified": False,
            }
        if configuration_result.get("configuration"):
            provider = dict(provider)
            provider["configuration"] = configuration_result["configuration"]
        credentials = self.credential_status(provider_id, environ)
        blocked_credential_states = {
            "CREDENTIALS_MISSING",
            "CREDENTIALS_EXPIRED",
            "CREDENTIAL_EXPIRY_INVALID",
            "INVALID_ENVIRONMENT",
            "MANUAL_CONNECTION_REQUIRED",
        }
        if credentials["status"] in blocked_credential_states:
            try:
                self.set_state(provider_id, "ERROR")
            except Exception:
                pass
            return {
                "status": credentials["status"],
                "provider": provider_id,
                "verified": False,
            }
        self.set_state(provider_id, "CONNECTING")
        started = time.monotonic()
        try:
            result = probe(dict(provider), timeout_seconds=float(timeout_seconds))
            if result is not True and not (isinstance(result, dict) and result.get("ok") is True):
                self.set_state(provider_id, "ERROR")
                return {
                    "status": "CONNECTION_FAILED",
                    "provider": provider_id,
                    "verified": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                }
        except Exception as exc:
            name = type(exc).__name__.casefold()
            if isinstance(exc, (TimeoutError,)) or "timeout" in name:
                status = "TIMEOUT"
            elif isinstance(exc, PermissionError) or any(term in name for term in ("auth", "unauthorized", "forbidden")):
                status = "AUTHENTICATION_OR_PERMISSION_FAILED"
            elif isinstance(exc, (ConnectionError, OSError)) or any(term in name for term in ("connection", "network")):
                status = "NETWORK_ERROR"
            else:
                status = "PROBE_FAILED"
            self.set_state(provider_id, "ERROR")
            return {
                "status": status,
                "provider": provider_id,
                "verified": False,
                "error_type": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        latency = round((time.monotonic() - started) * 1000, 2)
        saved = self._mark_connected_after_probe(provider_id)
        if not saved.get("saved"):
            return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
        return {
            "status": "CONNECTED",
            "provider": provider_id,
            "verified": True,
            "latency_ms": latency,
            "checked_at": saved["updated_at"],
        }

    def credential_status(self, provider_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}

        auth_type = str(provider.get("auth_type", "NONE")).upper()
        token_env = provider.get("token_env")
        if not token_env:
            token_env = {
                "huggingface": "HF_TOKEN",
                "replicate": "REPLICATE_API_TOKEN",
                "openai_image": "OPENAI_API_KEY",
                "openai_voice": "OPENAI_API_KEY",
                "openai_sora": "OPENAI_API_KEY",
                "whisper": "OPENAI_API_KEY",
                "google_image": "GOOGLE_API_KEY",
                "google_speech": "GOOGLE_API_KEY",
                "google_lyria": "GOOGLE_API_KEY",
                "google_veo": "GOOGLE_API_KEY",
                "recraft": "RECRAFT_API_KEY",
                "ideogram": "IDEOGRAM_API_KEY",
                "black_forest_labs": "BFL_API_KEY",
                "stability_ai": "STABILITY_API_KEY",
                "stability_audio": "STABILITY_API_KEY",
                "fal_ai": "FAL_KEY",
                "elevenlabs": "ELEVENLABS_API_KEY",
                "cartesia": "CARTESIA_API_KEY",
                "playht": "PLAYHT_API_KEY",
                "deepgram": "DEEPGRAM_API_KEY",
                "runway": "RUNWAY_API_KEY",
                "kling": "KLING_API_KEY",
                "luma": "LUMA_API_KEY",
                "minimax_hailuo": "MINIMAX_API_KEY",
                "heygen": "HEYGEN_API_KEY",
                "synthesia": "SYNTHESIA_API_KEY",
                "sync_labs": "SYNC_LABS_API_KEY",
                "hedra": "HEDRA_API_KEY",
                "assemblyai": "ASSEMBLYAI_API_KEY",
                "magnific": "MAGNIFIC_API_KEY",
                "adobe_firefly": "ADOBE_FIREFLY_API_KEY",
            }.get(provider_id)

        if auth_type in {"NONE", "PUBLIC", "ANONYMOUS"}:
            return {"provider": provider_id, "status": "NO_CREDENTIAL_REQUIRED", "credential_present": True}
        if not token_env:
            return {"provider": provider_id, "status": "MANUAL_CONNECTION_REQUIRED", "credential_present": False}

        if environ is not None and not isinstance(environ, dict):
            return {"provider": provider_id, "status": "INVALID_ENVIRONMENT", "credential_present": False}
        env = os.environ if environ is None else environ
        present = bool(str(env.get(str(token_env), "")).strip())
        if not present:
            return {
                "provider": provider_id,
                "status": "CREDENTIALS_MISSING",
                "credential_present": False,
                "credential_name": token_env,
            }
        expiry_env = provider.get("token_expiry_env")
        if expiry_env:
            raw_expiry = str(env.get(str(expiry_env), "")).strip()
            if raw_expiry:
                try:
                    expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                except ValueError:
                    return {
                        "provider": provider_id,
                        "status": "CREDENTIAL_EXPIRY_INVALID",
                        "credential_present": True,
                        "credential_name": token_env,
                    }
                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    return {
                        "provider": provider_id,
                        "status": "CREDENTIALS_EXPIRED",
                        "credential_present": True,
                        "credential_name": token_env,
                    }
                if remaining <= 300:
                    return {
                        "provider": provider_id,
                        "status": "CREDENTIALS_EXPIRING",
                        "credential_present": True,
                        "credential_name": token_env,
                        "expires_in_seconds": int(remaining),
                    }
        return {
            "provider": provider_id,
            "status": "CREDENTIALS_PRESENT",
            "credential_present": True,
            "credential_name": token_env,
        }

    def summary(self) -> dict[str, int]:
        with self._state_transaction():
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
