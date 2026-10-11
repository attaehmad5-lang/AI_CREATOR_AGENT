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
        self.validate_catalog_schema(self.catalog, raise_on_error=True)
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
                    if "capabilities" in provider and not (
                        isinstance(provider["capabilities"], list)
                        and all(isinstance(item, str) for item in provider["capabilities"])
                    ):
                        errors.append(f"providers[{index}].capabilities must be a list of strings")
                    sensitive_keys = {
                        str(key).casefold().replace("-", "_")
                        for key in provider
                    }
                    if any(
                        key in sensitive_keys
                        for key in ("api_key", "api_token", "access_token", "refresh_token", "password", "secret", "credential_value")
                    ):
                        errors.append(f"providers[{index}] contains a forbidden credential value field")
        result = {"valid": not errors, "errors": errors, "provider_count": len(catalog.get("providers", [])) if isinstance(catalog, dict) and isinstance(catalog.get("providers"), list) else 0}
        if errors and raise_on_error:
            raise ValueError("Invalid provider catalog: " + "; ".join(errors))
        return result

    def list_providers(self) -> list[dict[str, Any]]:
        providers = self.catalog.get("providers", [])
        if not isinstance(providers, list):
            raise ValueError("Provider catalog 'providers' must be a list")
        return [item for item in providers if isinstance(item, dict)]

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
            if normalized in {"token_env", "api_key_env", "secret_ref", "credential_env"}:
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
        providers = self.list_providers()
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

        existing = self.state["providers"].get(provider_id, {})
        record: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        record.update({
            "status": status,
            "verified": bool(verified),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        self.state["providers"][provider_id] = record
        self._save()
        return {"provider": provider_id, **record, "saved": True}

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

        if not isinstance(endpoint, str) or not endpoint.strip():
            return {"valid": False, "status": "INVALID_ENDPOINT"}
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
        if parsed.fragment:
            return {"valid": False, "status": "FRAGMENT_REJECTED"}
        normalized_host = hostname.casefold().rstrip(".")
        is_loopback = normalized_host == "localhost"
        try:
            is_loopback = is_loopback or ip_address(normalized_host).is_loopback
        except ValueError:
            pass
        if parsed.scheme == "http" and not (allow_loopback_http and is_loopback):
            return {"valid": False, "status": "HTTPS_REQUIRED"}
        if allowed_hosts is not None:
            normalized_allowed = {str(host).casefold().rstrip(".") for host in allowed_hosts}
            if normalized_host not in normalized_allowed:
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
                {item for item in allowed_hosts_raw if isinstance(item, str)}
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
            configuration["endpoint"] = endpoint.strip()
        if not configuration:
            return {"status": "NO_CONFIGURATION", "provider": provider_id, "saved": False}
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
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "verified": False}
        credentials = self.credential_status(provider_id, environ)
        if credentials["status"] == "CREDENTIALS_MISSING":
            return {"status": "CREDENTIALS_MISSING", "provider": provider_id, "verified": False}
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
        saved = self.set_state(provider_id, "CONNECTED", verified=True)
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
