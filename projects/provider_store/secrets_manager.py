"""Provider secret resolution and rotation through injected secure backends.

Secret values are fetched just-in-time, held in short-lived wipeable leases, and
never persisted by this module. External vault SDK clients are injected as backend
objects; each backend must enforce its own network timeout and service identity.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]{0,31}\Z")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z")
_PROVIDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_ALLOWED_PURPOSES = {"provider_execution", "connectivity_probe", "account_probe"}


class SecretManagerError(RuntimeError):
    """Safe, stable failure code; never includes backend exception text or secret data."""

    def __init__(self, status: str) -> None:
        self.status = status if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", status) else "SECRET_MANAGER_FAILED"
        super().__init__(self.status)


class SecretLease:
    """Short-lived secret buffer with a deliberately redacted representation."""

    __slots__ = ("_buffer", "_lock", "_closed", "backend", "version_id", "expires_at")

    def __init__(
        self,
        value: str,
        *,
        backend: str,
        version_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        self._buffer = bytearray(value.encode("utf-8"))
        self._lock = threading.RLock()
        self._closed = False
        self.backend = backend
        self.version_id = version_id
        self.expires_at = expires_at

    def reveal(self) -> str:
        with self._lock:
            if self._closed:
                raise SecretManagerError("SECRET_LEASE_CLOSED")
            return self._buffer.decode("utf-8")

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                for index in range(len(self._buffer)):
                    self._buffer[index] = 0
                self._buffer.clear()
                self._closed = True

    def __enter__(self) -> "SecretLease":
        if self.closed:
            raise SecretManagerError("SECRET_LEASE_CLOSED")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<SecretLease redacted>"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("secret leases cannot be serialized")


class EnvironmentSecretBackend:
    """Default read-only backend for env://VARIABLE references."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        if environ is not None and not isinstance(environ, dict):
            raise ValueError("environ must be a mapping")
        self.environ = os.environ if environ is None else environ

    def resolve_secret(
        self,
        reference: str,
        *,
        provider_id: str,
        tenant_id: str | None,
        purpose: str,
        version_id: str | None = None,
    ) -> str:
        parsed = urlsplit(reference)
        name = parsed.netloc
        value = self.environ.get(name)
        if not isinstance(value, str) or not value:
            raise SecretManagerError("SECRET_NOT_FOUND")
        return value

    def rotate_secret(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SecretManagerError("SECRET_ROTATION_UNSUPPORTED")


class ProviderSecretsManager:
    """Resolve references from environment or injected vault backends, without caching."""

    def __init__(
        self,
        *,
        backends: dict[str, Any] | None = None,
        environ: dict[str, str] | None = None,
        tenant_isolation_manager: Any | None = None,
        authorization_manager: Any | None = None,
        max_secret_bytes: int = 16384,
    ) -> None:
        if backends is not None and not isinstance(backends, dict):
            raise ValueError("backends must be a mapping")
        if isinstance(max_secret_bytes, bool) or not isinstance(max_secret_bytes, int) or not 1 <= max_secret_bytes <= 1_048_576:
            raise ValueError("max_secret_bytes must be within 1..1048576")
        if tenant_isolation_manager is not None and not callable(
            getattr(tenant_isolation_manager, "authorize_credential", None)
        ):
            raise ValueError("tenant isolation manager must authorize credential references")
        if authorization_manager is not None and not callable(
            getattr(authorization_manager, "authorize", None)
        ):
            raise ValueError("authorization manager must authorize operations")
        self.max_secret_bytes = max_secret_bytes
        self.tenant_isolation_manager = tenant_isolation_manager
        self.authorization_manager = authorization_manager
        self._lock = threading.RLock()
        self._backends: dict[str, Any] = {
            "env": EnvironmentSecretBackend(environ)
        }
        for scheme, backend in (backends or {}).items():
            self.register_backend(scheme, backend)

    @staticmethod
    def parse_reference(reference: Any) -> tuple[str, str] | None:
        if (
            not isinstance(reference, str)
            or not 1 <= len(reference) <= 512
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in reference)
            or "?" in reference
            or "#" in reference
            or "\\" in reference
        ):
            return None
        if "://" not in reference:
            return None
        scheme, target = reference.split("://", 1)
        if not _SCHEME_RE.fullmatch(scheme) or not _REFERENCE_RE.fullmatch(target):
            return None
        if target.startswith("/") or target.endswith("/") or "//" in target:
            return None
        segments = target.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            return None
        if scheme == "env":
            if "/" in target or not _ENV_NAME_RE.fullmatch(target):
                return None
        elif not target[0].isalnum():
            return None
        return scheme, target

    def register_backend(self, scheme: str, backend: Any) -> dict[str, Any]:
        if not isinstance(scheme, str) or not _SCHEME_RE.fullmatch(scheme) or scheme == "env":
            return {"status": "INVALID_SECRET_BACKEND_SCHEME", "registered": False}
        if not callable(getattr(backend, "resolve_secret", None)):
            return {"status": "INVALID_SECRET_BACKEND", "registered": False}
        with self._lock:
            self._backends[scheme] = backend
        return {"status": "SECRET_BACKEND_REGISTERED", "registered": True, "scheme": scheme}

    def inspect_reference(self, reference: str, *, provider_id: str | None = None) -> dict[str, Any]:
        parsed = self.parse_reference(reference)
        if parsed is None:
            return {"valid": False, "status": "INVALID_SECRET_REFERENCE"}
        scheme, _ = parsed
        with self._lock:
            backend = self._backends.get(scheme)
        if backend is None:
            return {"valid": False, "status": "SECRET_BACKEND_UNAVAILABLE", "scheme": scheme}
        if provider_id is not None and (
            not isinstance(provider_id, str) or not _PROVIDER_RE.fullmatch(provider_id)
        ):
            return {"valid": False, "status": "INVALID_PROVIDER_ID"}
        return {"valid": True, "status": "SECRET_BACKEND_READY", "scheme": scheme}

    def _authorize_reference(
        self,
        *,
        reference: str,
        tenant_id: str | None,
        principal_id: str | None,
    ) -> None:
        if self.tenant_isolation_manager is None:
            return
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(principal_id, str) or not principal_id:
            raise SecretManagerError("TENANT_CONTEXT_REQUIRED")
        try:
            result = self.tenant_isolation_manager.authorize_credential(
                tenant_id, reference, principal_id
            )
        except Exception:
            raise SecretManagerError("TENANT_CREDENTIAL_AUTHORIZATION_FAILED") from None
        if not isinstance(result, dict) or result.get("allowed") is not True:
            raise SecretManagerError("TENANT_CREDENTIAL_ACCESS_DENIED")

    @staticmethod
    def _validate_backend_response(response: Any, *, max_secret_bytes: int) -> tuple[str, str | None, str | None]:
        if isinstance(response, str):
            value, version_id, expires_at = response, None, None
        elif isinstance(response, dict) and set(response).issubset({"value", "version_id", "expires_at"}):
            value = response.get("value")
            version_id = response.get("version_id")
            expires_at = response.get("expires_at")
        else:
            raise SecretManagerError("INVALID_SECRET_BACKEND_RESPONSE")
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_secret_bytes:
            raise SecretManagerError("INVALID_SECRET_VALUE")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise SecretManagerError("INVALID_SECRET_VALUE")
        if version_id is not None and (
            not isinstance(version_id, str) or not 1 <= len(version_id) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in version_id)
        ):
            raise SecretManagerError("INVALID_SECRET_BACKEND_METADATA")
        if expires_at is not None:
            if not isinstance(expires_at, str):
                raise SecretManagerError("INVALID_SECRET_BACKEND_METADATA")
            try:
                parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except ValueError:
                raise SecretManagerError("INVALID_SECRET_BACKEND_METADATA") from None
            if parsed.tzinfo is None:
                raise SecretManagerError("INVALID_SECRET_BACKEND_METADATA")
            if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                raise SecretManagerError("SECRET_EXPIRED")
        return value, version_id, expires_at

    def resolve_secret(
        self,
        reference: str,
        *,
        provider_id: str,
        purpose: str,
        tenant_id: str | None = None,
        principal_id: str | None = None,
        version_id: str | None = None,
    ) -> SecretLease:
        parsed = self.parse_reference(reference)
        if parsed is None:
            raise SecretManagerError("INVALID_SECRET_REFERENCE")
        scheme, _ = parsed
        if not isinstance(provider_id, str) or not _PROVIDER_RE.fullmatch(provider_id):
            raise SecretManagerError("INVALID_PROVIDER_ID")
        if not isinstance(purpose, str) or purpose not in _ALLOWED_PURPOSES:
            raise SecretManagerError("INVALID_SECRET_PURPOSE")
        if version_id is not None and (
            not isinstance(version_id, str) or not 1 <= len(version_id) <= 128
        ):
            raise SecretManagerError("INVALID_SECRET_VERSION")
        self._authorize_reference(
            reference=reference, tenant_id=tenant_id, principal_id=principal_id
        )
        with self._lock:
            backend = self._backends.get(scheme)
        if backend is None:
            raise SecretManagerError("SECRET_BACKEND_UNAVAILABLE")
        try:
            response = backend.resolve_secret(
                reference,
                provider_id=provider_id,
                tenant_id=tenant_id,
                purpose=purpose,
                version_id=version_id,
            )
            value, resolved_version, expires_at = self._validate_backend_response(
                response, max_secret_bytes=self.max_secret_bytes
            )
            return SecretLease(
                value,
                backend=scheme,
                version_id=resolved_version,
                expires_at=expires_at,
            )
        except SecretManagerError:
            raise
        except Exception as exc:
            raise SecretManagerError(
                "SECRET_BACKEND_TIMEOUT" if isinstance(exc, TimeoutError) else "SECRET_BACKEND_FAILED"
            ) from None

    def rotate_secret(
        self,
        reference: str,
        *,
        provider_id: str,
        idempotency_key: str,
        principal_id: str | None,
        confirmed: bool,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        parsed = self.parse_reference(reference)
        if parsed is None:
            return {"status": "INVALID_SECRET_REFERENCE", "rotated": False}
        scheme, _ = parsed
        if not isinstance(provider_id, str) or not _PROVIDER_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "rotated": False}
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            return {"status": "INVALID_IDEMPOTENCY_KEY", "rotated": False}
        if confirmed is not True:
            return {"status": "ROTATION_CONFIRMATION_REQUIRED", "rotated": False}
        if self.authorization_manager is None:
            return {"status": "AUTHORIZATION_MANAGER_REQUIRED", "rotated": False}
        try:
            authorization = self.authorization_manager.authorize(
                principal_id, "provider.credentials.rotate"
            )
        except Exception:
            return {"status": "ROTATION_AUTHORIZATION_FAILED", "rotated": False}
        if not isinstance(authorization, dict) or authorization.get("allowed") is not True:
            return {"status": "ROTATION_ACCESS_DENIED", "rotated": False}
        try:
            self._authorize_reference(
                reference=reference, tenant_id=tenant_id, principal_id=principal_id
            )
        except SecretManagerError as exc:
            return {"status": exc.status, "rotated": False}
        with self._lock:
            backend = self._backends.get(scheme)
        rotate = getattr(backend, "rotate_secret", None) if backend is not None else None
        if not callable(rotate):
            return {"status": "SECRET_ROTATION_UNSUPPORTED", "rotated": False}
        try:
            result = rotate(
                reference,
                provider_id=provider_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            return {
                "status": "SECRET_ROTATION_TIMEOUT" if isinstance(exc, TimeoutError) else "SECRET_ROTATION_FAILED",
                "rotated": False,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or result.get("rotated") is not True:
            return {"status": "INVALID_ROTATION_RESPONSE", "rotated": False}
        version_id = result.get("version_id")
        rotated_at = result.get("rotated_at")
        if version_id is not None and (
            not isinstance(version_id, str) or not 1 <= len(version_id) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in version_id)
        ):
            return {"status": "INVALID_ROTATION_METADATA", "rotated": False}
        if rotated_at is not None:
            if not isinstance(rotated_at, str):
                return {"status": "INVALID_ROTATION_METADATA", "rotated": False}
            try:
                parsed_at = datetime.fromisoformat(rotated_at.replace("Z", "+00:00"))
            except ValueError:
                return {"status": "INVALID_ROTATION_METADATA", "rotated": False}
            if parsed_at.tzinfo is None:
                return {"status": "INVALID_ROTATION_METADATA", "rotated": False}
        return {
            "status": "SECRET_ROTATED",
            "rotated": True,
            "provider_id": provider_id,
            "backend": scheme,
            **({"version_id": version_id} if version_id is not None else {}),
            **({"rotated_at": rotated_at} if rotated_at is not None else {}),
        }
