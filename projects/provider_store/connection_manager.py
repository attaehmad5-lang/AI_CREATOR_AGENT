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

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG = MODULE_DIR / "provider_catalog.json"
MASTER_CATALOG = MODULE_DIR / "provider_catalog_master.json"
DEFAULT_STATE = MODULE_DIR / "connection_state.json"


class ProviderAuthenticationVerificationRequired(PermissionError):
    """The generic HTTP probe needs a documented auth-protected endpoint."""



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
        # Constructor reads must use the same interprocess locks as writers. On
        # Windows, an unlocked read can race with atomic replacement and fail with
        # a transient sharing violation while another process registers a provider.
        catalog_lock_path = self.catalog_path.with_name(self.catalog_path.name + ".lock")
        with interprocess_file_lock(catalog_lock_path):
            self.catalog = self._load_json(self.catalog_path)
        self.validate_catalog_schema(self.catalog, raise_on_error=True)
        state_lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        with interprocess_file_lock(state_lock_path):
            self.state = self._load_json(
                self.state_path,
                default={"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}},
            )
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

    @classmethod
    def restore_catalog_from_master(
        cls,
        catalog_path: str | os.PathLike[str] | None = None,
        master_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Restore only a missing/invalid catalog from a separately validated master copy."""
        target = Path(catalog_path) if catalog_path is not None else DEFAULT_CATALOG
        master = Path(master_path) if master_path is not None else MASTER_CATALOG
        if target.resolve() == master.resolve():
            return {"status": "RECOVERY_TARGET_EQUALS_MASTER", "restored": False}
        lock_path = target.with_name(target.name + ".lock")
        with interprocess_file_lock(lock_path):
            if target.exists():
                try:
                    current = cls._load_json(target)
                    validation = cls.validate_catalog_schema(current)
                except (OSError, ValueError, json.JSONDecodeError):
                    validation = {"valid": False, "errors": ["target catalog is unreadable"]}
                if validation.get("valid") is True:
                    return {"status": "TARGET_CATALOG_VALID", "restored": False}
            try:
                backup = cls._load_json(master)
                validation = cls.validate_catalog_schema(backup)
            except (OSError, ValueError, json.JSONDecodeError):
                return {"status": "MASTER_CATALOG_INVALID", "restored": False}
            if validation.get("valid") is not True:
                return {
                    "status": "MASTER_CATALOG_INVALID",
                    "restored": False,
                    "errors": validation.get("errors", []),
                }
            try:
                cls._atomic_write_json(target, backup)
            except OSError as exc:
                return {
                    "status": "CATALOG_RECOVERY_WRITE_FAILED",
                    "restored": False,
                    "error_type": type(exc).__name__,
                }
            return {
                "status": "RESTORED_FROM_MASTER",
                "restored": True,
                "provider_count": validation.get("provider_count", 0),
            }

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
            atomic_replace_with_retry(temp_name, path)
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
        from urllib.parse import urlsplit

        errors: list[str] = []
        if not isinstance(catalog, dict):
            errors.append("catalog must be an object")
        else:
            if "version" in catalog and (
                not isinstance(catalog.get("version"), (str, int, float))
                or isinstance(catalog.get("version"), bool)
                or not re.fullmatch(r"\d+(?:\.\d+){0,2}", str(catalog.get("version")))
            ):
                errors.append("catalog.version must be a numeric version such as '2.0' or '3.0'")
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
                    elif (
                        len(provider_id) > 100
                        or any(
                            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                            for char in provider_id
                        )
                    ):
                        errors.append(f"providers[{index}].id contains invalid characters or exceeds 100 characters")
                    elif provider_id in seen:
                        errors.append(f"duplicate provider id: {provider_id}")
                    else:
                        seen.add(provider_id)
                    for key in ("name", "category", "auth_type", "connection_type"):
                        if key in provider and not isinstance(provider[key], str):
                            errors.append(f"providers[{index}].{key} must be a string")
                    auth_types = {
                        "NONE", "API_KEY", "API_TOKEN", "ACCOUNT", "MANUAL",
                        "MANUAL_CONNECT", "OAUTH_BROWSER", "ACCOUNT_OR_API", "ACCOUNT_OR_LOCAL",
                        "API_KEY_OR_ACCOUNT", "API_KEY_OR_SERVICE_ACCOUNT",
                    }
                    connection_types = {
                        "API", "ACCOUNT_OR_API", "API_OR_ACCOUNT", "LOCAL_OR_ACCOUNT",
                        "MANUAL_CONNECT", "BROWSER_SESSION", "LOCAL",
                    }
                    auth_type = provider.get("auth_type")
                    if isinstance(auth_type, str) and auth_type.upper() not in auth_types:
                        errors.append(f"providers[{index}].auth_type is unsupported")
                    connection_type = provider.get("connection_type")
                    if isinstance(connection_type, str) and connection_type.upper() not in connection_types:
                        errors.append(f"providers[{index}].connection_type is unsupported")
                    if "enabled" in provider and not isinstance(provider["enabled"], bool):
                        errors.append(f"providers[{index}].enabled must be boolean")
                    if "allowed_hosts" in provider:
                        hosts = provider["allowed_hosts"]
                        if not isinstance(hosts, list) or not hosts or len(hosts) > 100:
                            errors.append(f"providers[{index}].allowed_hosts must be a non-empty list of at most 100 hostnames")
                        elif (
                            any(
                                not isinstance(host, str)
                                or not host
                                or len(host) > 253
                                or not re.fullmatch(
                                    r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?",
                                    host,
                                )
                                or ".." in host
                                or any(
                                    not label
                                    or len(label) > 63
                                    or label.startswith("-")
                                    or label.endswith("-")
                                    for label in host.rstrip(".").split(".")
                                )
                                for host in hosts
                            )
                            or len({host.casefold().rstrip(".") for host in hosts if isinstance(host, str)}) != len(hosts)
                        ):
                            errors.append(f"providers[{index}].allowed_hosts contains invalid or duplicate hostnames")
                    version = catalog.get("version", "0") if isinstance(catalog, dict) else "0"
                    try:
                        catalog_major_version = int(str(version).split(".", 1)[0])
                    except (TypeError, ValueError):
                        catalog_major_version = 0
                    if catalog_major_version >= 3:
                        for required_field in ("id", "name", "category", "auth_type", "connection_type"):
                            value = provider.get(required_field)
                            if not isinstance(value, str) or not value.strip():
                                errors.append(
                                    f"providers[{index}].{required_field} is required and must be non-empty"
                                )
                        for required_flag in ("enabled", "subscription_required"):
                            if not isinstance(provider.get(required_flag), bool):
                                errors.append(
                                    f"providers[{index}].{required_flag} is required and must be boolean"
                                )
                    if (
                        catalog_major_version >= 3
                        and provider.get("enabled") is True
                        and isinstance(connection_type, str)
                        and connection_type.upper() == "API"
                        and not provider.get("allowed_hosts")
                    ):
                        errors.append(
                            f"providers[{index}].allowed_hosts is required for enabled API providers"
                        )
                    for url_field in ("official_api_url", "subscription_url"):
                        if url_field in provider:
                            raw_url = provider[url_field]
                            try:
                                parsed_url = urlsplit(raw_url) if isinstance(raw_url, str) else None
                                _ = parsed_url.port if parsed_url is not None else None
                            except ValueError:
                                parsed_url = None
                            if (
                                not isinstance(raw_url, str)
                                or not raw_url.strip()
                                or len(raw_url) > 2048
                                or any(ord(char) < 32 or ord(char) == 127 for char in raw_url)
                                or parsed_url is None
                                or parsed_url.scheme != "https"
                                or not parsed_url.hostname
                                or parsed_url.username is not None
                                or parsed_url.password is not None
                            ):
                                errors.append(
                                    f"providers[{index}].{url_field} must be a safe HTTPS URL without embedded credentials"
                                )
                    if "auth_header_name" in provider:
                        safe_auth_headers = {
                            "authorization", "x-api-key", "api-key", "xi-api-key",
                            "x-goog-api-key", "cartesia-api-key", "x-auth-token", "x-key",
                        }
                        header = provider["auth_header_name"]
                        if (
                            not isinstance(header, str)
                            or header.casefold() not in safe_auth_headers
                        ):
                            errors.append(f"providers[{index}].auth_header_name is not permitted")
                    if "auth_scheme" in provider and (
                        not isinstance(provider["auth_scheme"], str)
                        or provider["auth_scheme"].casefold() not in {"", "bearer", "key", "token", "basic"}
                    ):
                        errors.append(f"providers[{index}].auth_scheme is not permitted")
                    if "subscription_required" in provider and not isinstance(provider["subscription_required"], bool):
                        errors.append(f"providers[{index}].subscription_required must be boolean")
                    if "zero_cost_allowed" in provider and not isinstance(provider["zero_cost_allowed"], bool):
                        errors.append(f"providers[{index}].zero_cost_allowed must be boolean")
                    if provider.get("subscription_required") is True and provider.get("zero_cost_allowed") is True:
                        errors.append(
                            f"providers[{index}] cannot require a subscription and declare zero_cost_allowed"
                        )
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
                        or any(
                            marker in provider["secret_ref"].casefold()
                            for marker in ("token=", "api_key=", "password=", "secret=", "bearer ")
                        )
                    ):
                        errors.append(f"providers[{index}].secret_ref must be a safe non-empty reference")
                    if "capabilities" in provider:
                        capabilities = provider["capabilities"]
                        if (
                            not isinstance(capabilities, list)
                            or len(capabilities) > 100
                            or any(
                                not isinstance(item, str)
                                or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", item)
                                for item in capabilities
                            )
                            or len({item.casefold() for item in capabilities if isinstance(item, str)}) != len(capabilities)
                        ):
                            errors.append(
                                f"providers[{index}].capabilities must contain unique lowercase identifiers "
                                "of at most 80 characters (maximum 100)"
                            )
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
        """Validate only the adapter methods required by this provider's declared capabilities."""
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "valid": False}
        category = str(provider.get("category", "")).casefold().replace("-", "_").replace(" ", "_")
        category_contracts: dict[str, tuple[str, ...]] = {
            "image": ("generate_image",),
            "video": ("generate_video",),
            "voice": ("generate_speech",),
            "audio": ("generate_audio",),
            "music": ("generate_music",),
            "text": ("generate_text",),
            "language_model": ("generate_text",),
            "transcription": ("transcribe",),
            "translation": ("translate",),
            "dubbing": ("dub",),
            "image_editing": ("edit_image",),
            "advanced_image_editing": ("edit_image",),
            "upscaling": ("upscale_image",),
            "advanced_upscaling": ("upscale_image",),
            "lipsync": ("generate_lipsync",),
            "script_story": ("generate_text",),
            "advanced_transcription": ("transcribe",),
            "advanced_audio": (),
            "video_editing": ("edit_video",),
            "advanced_video_editing": ("edit_video",),
        }
        if category not in category_contracts:
            return {
                "status": "UNSUPPORTED_MODALITY",
                "provider": provider_id,
                "valid": False,
                "contract_version": "1.0",
            }

        capabilities = provider.get("capabilities", [])
        if isinstance(capabilities, str):
            capabilities = [capabilities]
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item.strip() for item in capabilities
        ) or (category == "advanced_audio" and not capabilities):
            return {
                "status": "INVALID_CAPABILITY_METADATA",
                "provider": provider_id,
                "valid": False,
                "contract_version": "1.0",
            }

        # A capability is a declared contract, not proof that the provider works.
        # Every declared capability must have a corresponding callable method.
        capability_methods: dict[str, tuple[str, ...]] = {
            "image_generation": ("generate_image",),
            "video_generation": ("generate_video",),
            "text_to_speech": ("generate_speech",),
            "voice_generation": ("generate_speech",),
            "voice_cloning": ("generate_speech",),
            "voice_dubbing": ("dub",),
            "audio_generation": ("generate_audio",),
            "sound_effect_generation": ("generate_audio",),
            "music_generation": ("generate_music",),
            "audio_transcription": ("transcribe",),
            "speech_to_text": ("transcribe",),
            "speaker_detection": ("transcribe",),
            "subtitle_generation": ("transcribe",),
            "caption_timing": ("transcribe",),
            "transcript_cleanup": ("transcribe",),
            "story_generation": ("generate_text",),
            "screenplay": ("generate_text",),
            "scene_breakdown": ("generate_text",),
            "character_development": ("generate_text",),
            "dialogue_writing": ("generate_text",),
            "storyboard_planning": ("generate_text",),
            "image_editing": ("edit_image",),
            "inpainting": ("edit_image",),
            "outpainting": ("edit_image",),
            "object_removal": (("edit_video" if "video" in category else "edit_image"),),
            "background_removal": ("edit_image",),
            "background_replacement": (("edit_video" if "video" in category else "edit_image"),),
            "relighting": ("edit_image",),
            "image_restoration": (("upscale_image" if "upscal" in category else "edit_image"),),
            "image_upscaling": ("upscale_image",),
            "video_upscaling": ("upscale_video",),
            "face_restoration": ("upscale_image",),
            "detail_enhancement": ("upscale_image",),
            "old_photo_restoration": ("upscale_image",),
            "denoise": (("upscale_image" if "upscal" in category else "edit_audio"),),
            "lip_sync": ("generate_lipsync",),
            "audio_enhancement": ("edit_audio",),
            "noise_removal": ("edit_audio",),
            "audio_editing": ("edit_audio",),
            "video_editing": ("edit_video",),
            "scene_detection": ("edit_video",),
            "color_grading": ("edit_video",),
            "video_restoration": ("edit_video",),
        }

        requirements: list[tuple[str, ...]] = [ (name,) for name in category_contracts[category] ]
        for capability in dict.fromkeys(capabilities):
            required = capability_methods.get(capability)
            if required is None:
                return {
                    "status": "UNSUPPORTED_CAPABILITY",
                    "provider": provider_id,
                    "valid": False,
                    "unsupported_capabilities": [capability],
                    "contract_version": "1.0",
                }
            if required not in requirements:
                requirements.append(required)

        def has_callable(name: str) -> bool:
            try:
                return callable(getattr(instance, name, None))
            except Exception:
                return False

        missing = [
            " or ".join(alternatives)
            for alternatives in requirements
            if not any(has_callable(name) for name in alternatives)
        ]
        return {
            "status": "VALID" if not missing else "CONTRACT_VIOLATION",
            "provider": provider_id,
            "valid": not missing,
            "missing_methods": missing,
            "contract_version": "2.0",
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
        try:
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
        except Exception as exc:
            return {
                "status": "STATE_READ_FAILED",
                "verified": False,
                "error_type": type(exc).__name__,
            }

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
        if any(ord(char) < 32 or ord(char) == 127 for char in endpoint):
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
        if port == 0:
            return {"valid": False, "status": "INVALID_PORT"}
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
        probe_endpoint: str | None = None,
        authentication_required: bool | None = None,
    ) -> dict[str, Any]:
        """Persist non-secret generation/probe endpoints and model settings.

        Set authentication_required=True only when the configured endpoint is
        documented to require and validate the provider credential.
        """
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "saved": False}
        configuration: dict[str, Any] = {}
        if authentication_required is not None:
            if not isinstance(authentication_required, bool):
                return {"status": "INVALID_AUTHENTICATION_POLICY", "provider": provider_id, "saved": False}
            configuration["authentication_required"] = authentication_required
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
        if probe_endpoint is not None:
            allowed_hosts_raw = provider.get("allowed_hosts")
            allowed_hosts = (
                {item for item in allowed_hosts_raw if isinstance(item, str) and item.strip()}
                if isinstance(allowed_hosts_raw, list) else None
            )
            validation = self.validate_endpoint(probe_endpoint, allowed_hosts=allowed_hosts)
            if not validation["valid"]:
                return {
                    "status": validation["status"],
                    "provider": provider_id,
                    "saved": False,
                }
            from urllib.parse import urlsplit
            if urlsplit(probe_endpoint).query:
                return {"status": "QUERY_PARAMETERS_REJECTED", "provider": provider_id, "saved": False}
            if not allowed_hosts:
                return {"status": "HOST_ALLOWLIST_REQUIRED", "provider": provider_id, "saved": False}
            configuration["probe_endpoint"] = probe_endpoint.strip()
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
            if self._contains_credential_value_field(raw):
                return {"status": "SENSITIVE_CONFIGURATION_REJECTED", "provider": provider_id, "configuration": {}}
            configuration = self._sanitize_public_data(raw)
        if self._contains_credential_value_field(configuration):
            return {"status": "SENSITIVE_CONFIGURATION_REJECTED", "provider": provider_id, "configuration": {}}
        authentication_required = configuration.get("authentication_required")
        if authentication_required is not None and not isinstance(authentication_required, bool):
            return {"status": "INVALID_AUTHENTICATION_POLICY", "provider": provider_id, "configuration": {}}
        model = configuration.get("model")
        if model is not None and (
            not isinstance(model, str) or not model.strip() or len(model) > 200
        ):
            return {"status": "INVALID_MODEL", "provider": provider_id, "configuration": {}}
        if isinstance(model, str) and any(
            marker in model.casefold()
            for marker in ("api_key=", "token=", "password=", "secret=", "bearer ")
        ):
            return {"status": "SENSITIVE_CONFIGURATION_REJECTED", "provider": provider_id, "configuration": {}}
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
        probe_endpoint = configuration.get("probe_endpoint")
        if probe_endpoint is not None:
            allowed_hosts_raw = provider.get("allowed_hosts")
            allowed_hosts = (
                {item for item in allowed_hosts_raw if isinstance(item, str) and item.strip()}
                if isinstance(allowed_hosts_raw, list) else None
            )
            if not allowed_hosts:
                return {"status": "HOST_ALLOWLIST_REQUIRED", "provider": provider_id, "configuration": {}}
            validation = self.validate_endpoint(probe_endpoint, allowed_hosts=allowed_hosts)
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
        """Run an explicitly injected probe; only persisted success marks CONNECTED."""
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

        def save_state(status: str) -> bool:
            try:
                result = self.set_state(provider_id, status)
            except Exception:
                return False
            return isinstance(result, dict) and result.get("saved") is True

        try:
            configuration_result = self.get_provider_configuration(provider_id)
        except Exception as exc:
            return {
                "status": "STATE_READ_FAILED", "provider": provider_id,
                "verified": False, "error_type": type(exc).__name__,
            }
        if configuration_result.get("status") not in {"CONFIGURED", "NO_CONFIGURATION"}:
            if not save_state("ERROR"):
                return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
            return {
                "status": configuration_result.get("status", "INVALID_CONFIGURATION"),
                "provider": provider_id, "verified": False,
            }
        if configuration_result.get("configuration"):
            provider = dict(provider)
            provider["configuration"] = configuration_result["configuration"]

        credentials = self.credential_status(provider_id, environ)
        blocked_credential_states = {
            "CREDENTIALS_MISSING", "CREDENTIALS_EXPIRED", "CREDENTIAL_EXPIRY_INVALID",
            "CREDENTIAL_EXPIRY_UNKNOWN", "INVALID_ENVIRONMENT", "MANUAL_CONNECTION_REQUIRED",
        }
        if credentials["status"] in blocked_credential_states:
            if not save_state("ERROR"):
                return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
            return {
                "status": credentials["status"], "provider": provider_id, "verified": False,
            }
        if not save_state("CONNECTING"):
            return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}

        started = time.monotonic()
        try:
            result = probe(dict(provider), timeout_seconds=float(timeout_seconds))
            if result is not True and not (isinstance(result, dict) and result.get("ok") is True):
                if not save_state("ERROR"):
                    return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
                return {
                    "status": "CONNECTION_FAILED", "provider": provider_id, "verified": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                }
            configured = provider.get("configuration", {})
            auth_type = str(provider.get("auth_type", "NONE")).strip().upper()
            credentialed_auth = auth_type not in {"NONE", "PUBLIC", "ANONYMOUS", "ACCOUNT", "MANUAL", "MANUAL_CONNECT", "OAUTH_BROWSER"}
            if (
                credentialed_auth
                and isinstance(configured, dict)
                and configured.get("authentication_required") is True
                and not (isinstance(result, dict) and result.get("authentication_verified") is True)
            ):
                if not save_state("ERROR"):
                    return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
                return {
                    "status": "AUTHENTICATION_NOT_VERIFIED",
                    "provider": provider_id,
                    "verified": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                }
        except Exception as exc:
            name = type(exc).__name__.casefold()
            http_status = getattr(exc, "status_code", None)
            retry_after = getattr(exc, "retry_after", None)
            if (
                not isinstance(http_status, int) or isinstance(http_status, bool)
                or not 100 <= http_status <= 599
            ):
                http_status = None
            if not isinstance(retry_after, str) or len(retry_after) > 128 or any(
                ord(char) < 32 or ord(char) == 127 for char in retry_after
            ):
                retry_after = None
            safe_probe_configuration_statuses = {
                "PROBE_ENDPOINT_NOT_CONFIGURED", "HOST_ALLOWLIST_REQUIRED",
                "INVALID_ENDPOINT", "INVALID_HOST_ALLOWLIST", "HTTPS_REQUIRED",
                "INVALID_PORT", "EMBEDDED_CREDENTIALS_REJECTED",
                "QUERY_PARAMETERS_REJECTED", "FRAGMENT_REJECTED",
                "NON_PUBLIC_ADDRESS_REJECTED", "HOST_NOT_ALLOWLISTED",
                "INVALID_AUTH_TYPE",
            }
            if isinstance(exc, ProviderAuthenticationVerificationRequired):
                status = "AUTHENTICATION_VERIFICATION_REQUIRED"
            elif isinstance(exc, ValueError) and str(exc) in safe_probe_configuration_statuses:
                status = str(exc)
            elif http_status in {401, 403}:
                status = "AUTHENTICATION_OR_PERMISSION_FAILED"
            elif http_status == 429:
                status = "RATE_LIMITED"
            elif http_status is not None and 500 <= http_status <= 599:
                status = "PROVIDER_UNAVAILABLE"
            elif http_status is not None and 400 <= http_status <= 499:
                status = "PROVIDER_HTTP_ERROR"
            elif isinstance(exc, TimeoutError) or "timeout" in name:
                status = "TIMEOUT"
            elif isinstance(exc, PermissionError) or any(
                term in name for term in ("auth", "unauthorized", "forbidden")
            ):
                status = "AUTHENTICATION_OR_PERMISSION_FAILED"
            elif isinstance(exc, (ConnectionError, OSError)) or any(
                term in name for term in ("connection", "network")
            ):
                status = "NETWORK_ERROR"
            else:
                status = "PROBE_FAILED"
            if not save_state("ERROR"):
                return {
                    "status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False,
                    "error_type": type(exc).__name__,
                }
            result = {
                "status": status, "provider": provider_id, "verified": False,
                "error_type": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
            if http_status is not None:
                result["http_status"] = http_status
            if retry_after is not None:
                result["retry_after"] = retry_after
            return result

        try:
            saved = self._mark_connected_after_probe(provider_id)
        except Exception:
            saved = {"saved": False}
        if not isinstance(saved, dict) or saved.get("saved") is not True:
            return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
        return {
            "status": "CONNECTED", "provider": provider_id, "verified": True,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "checked_at": saved["updated_at"],
        }

    def verify_manual_connection(
        self,
        provider_id: str,
        manual_session: Any,
        probe: Any,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Verify an externally authorized account session without persisting or returning it.

        The injected provider-specific probe must validate the live session. The session
        object is held only for this call; it is never copied into catalog/state or logs.
        """
        import time

        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "provider": provider_id, "verified": False}
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id, "verified": False}
        if provider.get("enabled") is False:
            return {"status": "DISABLED", "provider": provider_id, "verified": False}
        auth_type = str(provider.get("auth_type", "NONE")).upper()
        manual_modes = {"ACCOUNT", "MANUAL", "MANUAL_CONNECT", "OAUTH_BROWSER"}
        if not (
            auth_type in manual_modes
            or any(mode in auth_type for mode in ("ACCOUNT", "MANUAL", "OAUTH_BROWSER", "SERVICE_ACCOUNT", "LOCAL"))
        ):
            return {"status": "MANUAL_CONNECTION_NOT_APPLICABLE", "provider": provider_id, "verified": False}
        if manual_session is None:
            return {"status": "MANUAL_SESSION_REQUIRED", "provider": provider_id, "verified": False}
        if not callable(probe):
            return {"status": "INVALID_PROBE", "provider": provider_id, "verified": False}
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            return {"status": "INVALID_TIMEOUT", "provider": provider_id, "verified": False}

        try:
            configuration_result = self.get_provider_configuration(provider_id)
        except Exception as exc:
            return {
                "status": "STATE_READ_FAILED", "provider": provider_id,
                "verified": False, "error_type": type(exc).__name__,
            }
        if configuration_result.get("status") not in {"CONFIGURED", "NO_CONFIGURATION"}:
            return {
                "status": configuration_result.get("status", "INVALID_CONFIGURATION"),
                "provider": provider_id, "verified": False,
            }
        if configuration_result.get("configuration"):
            provider = dict(provider)
            provider["configuration"] = configuration_result["configuration"]

        try:
            saved_state = self.set_state(provider_id, "CONNECTING")
        except Exception:
            saved_state = {"saved": False}
        if not isinstance(saved_state, dict) or saved_state.get("saved") is not True:
            return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}

        started = time.monotonic()
        try:
            result = probe(dict(provider), session=manual_session, timeout_seconds=float(timeout_seconds))
            if result is not True and not (isinstance(result, dict) and result.get("ok") is True):
                try:
                    error_state = self.set_state(provider_id, "ERROR")
                except Exception:
                    error_state = {"saved": False}
                if not isinstance(error_state, dict) or error_state.get("saved") is not True:
                    return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
                return {
                    "status": "MANUAL_CONNECTION_FAILED", "provider": provider_id, "verified": False,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                }
        except Exception as exc:
            try:
                error_state = self.set_state(provider_id, "ERROR")
            except Exception:
                error_state = {"saved": False}
            if not isinstance(error_state, dict) or error_state.get("saved") is not True:
                return {
                    "status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False,
                    "error_type": type(exc).__name__,
                }
            name = type(exc).__name__.casefold()
            status = (
                "TIMEOUT" if isinstance(exc, TimeoutError) or "timeout" in name
                else "AUTHENTICATION_OR_PERMISSION_FAILED" if isinstance(exc, PermissionError)
                else "NETWORK_ERROR" if isinstance(exc, (ConnectionError, OSError))
                else "MANUAL_CONNECTION_FAILED"
            )
            return {
                "status": status, "provider": provider_id, "verified": False,
                "error_type": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }

        try:
            saved = self._mark_connected_after_probe(provider_id)
        except Exception:
            saved = {"saved": False}
        if not isinstance(saved, dict) or saved.get("saved") is not True:
            return {"status": "STATE_SAVE_FAILED", "provider": provider_id, "verified": False}
        return {
            "status": "CONNECTED", "provider": provider_id, "verified": True,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "checked_at": saved["updated_at"],
        }

    def credential_status(self, provider_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}

        auth_type = str(provider.get("auth_type", "NONE")).upper()
        if auth_type in {"ACCOUNT", "MANUAL", "MANUAL_CONNECT", "OAUTH_BROWSER"}:
            return {
                "provider": provider_id,
                "status": "MANUAL_CONNECTION_REQUIRED",
                "credential_present": False,
            }
        token_env = provider.get("token_env") or provider.get("api_key_env") or provider.get("credential_env")
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
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", token_env):
            return {"provider": provider_id, "status": "INVALID_ENVIRONMENT", "credential_present": False}

        if environ is not None and not isinstance(environ, dict):
            return {"provider": provider_id, "status": "INVALID_ENVIRONMENT", "credential_present": False}
        env = os.environ if environ is None else environ
        raw_token = env.get(token_env)
        present = isinstance(raw_token, str) and bool(raw_token.strip())
        if not present:
            return {
                "provider": provider_id,
                "status": "CREDENTIALS_MISSING",
                "credential_present": False,
                "credential_name": token_env,
            }
        expiry_env = provider.get("token_expiry_env")
        if expiry_env:
            expiry_value = env.get(expiry_env)
            raw_expiry = expiry_value.strip() if isinstance(expiry_value, str) else ""
            if not raw_expiry:
                return {
                    "provider": provider_id,
                    "status": "CREDENTIAL_EXPIRY_UNKNOWN",
                    "credential_present": True,
                    "credential_name": token_env,
                }
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
