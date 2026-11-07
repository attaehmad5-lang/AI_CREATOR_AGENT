"""Credential protection and security policy (section I).

Rules enforced here:
* the registry never stores a secret value in catalog/overlay/audit/log/event data;
* secrets live in the OS environment (referenced by name) or in an encrypted vault;
* every credential access is authorized (ProviderAuthorizationManager) and audited;
* TLS is verified (certificate chain + hostname) and plain HTTP is refused.
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..connection_manager import ConnectionManager
from ..interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from ..provider_authorization import ProviderAuthorizationManager
from .core import RegistryCore
from .storage import RegistryStore

MASTER_KEY_ENV = "AICA_REGISTRY_MASTER_KEY"
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
AUDIT_LIMIT = 2000

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:hf|ghp|gho|xox[abp])_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*[^\s,;\"']{6,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
]


class VaultUnavailable(RuntimeError):
    pass


class SecretValue:
    """Holds a secret; str()/repr()/format never reveal it."""
    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "SecretValue(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __reduce__(self):  # prevent accidental pickling of the secret
        raise TypeError("SecretValue cannot be pickled")


def redact(text: str) -> str:
    out = str(text)
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(lambda m: (m.group(1) + "=***") if m.lastindex else "***REDACTED***", out)
    return out


def find_secrets(value: Any, path: str = "$") -> list[str]:
    """Return JSON-paths of string values that look like secrets (never the values)."""
    hits: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            hits.extend(find_secrets(v, f"{path}.{k}"))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hits.extend(find_secrets(v, f"{path}[{i}]"))
    elif isinstance(value, str):
        if any(p.search(value) for p in SECRET_PATTERNS):
            hits.append(path)
    return hits


class SecretVault:
    """Fernet-encrypted secret store. The master key comes from the environment, never from disk."""

    def __init__(self, path: str | os.PathLike[str], *, environ: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as exc:
            raise VaultUnavailable("the 'cryptography' package is required for encrypted storage") from exc
        self._Fernet, self._InvalidToken = Fernet, InvalidToken
        self.path = Path(path)
        key = (os.environ if environ is None else environ).get(MASTER_KEY_ENV)
        if not key:
            raise VaultUnavailable(f"{MASTER_KEY_ENV} is not set")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise VaultUnavailable("master key is not a valid Fernet key") from exc
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    @staticmethod
    def generate_master_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"entries": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".vault.", suffix=".tmp", dir=str(self.path.parent), text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        atomic_replace_with_retry(temp, self.path)

    def put(self, provider_id: str, name: str, value: str) -> dict[str, Any]:
        if not isinstance(value, str) or not value:
            return {"status": "INVALID_SECRET"}
        key = f"{provider_id}::{name}"
        with interprocess_file_lock(self._lock_path):
            data = self._load()
            previous = data["entries"].get(key)
            now = self._clock().isoformat()
            data["entries"][key] = {"token": self._fernet.encrypt(value.encode()).decode(),
                                    "version": (previous["version"] + 1) if previous else 1,
                                    "created_at": previous["created_at"] if previous else now,
                                    "rotated_at": now if previous else None}
            self._save(data)
            return {"status": "STORED", "version": data["entries"][key]["version"]}

    def get(self, provider_id: str, name: str) -> SecretValue | None:
        with interprocess_file_lock(self._lock_path):
            entry = self._load()["entries"].get(f"{provider_id}::{name}")
        if entry is None:
            return None
        try:
            return SecretValue(self._fernet.decrypt(entry["token"].encode()).decode())
        except self._InvalidToken:
            return None

    def meta(self, provider_id: str, name: str) -> dict[str, Any] | None:
        with interprocess_file_lock(self._lock_path):
            entry = self._load()["entries"].get(f"{provider_id}::{name}")
        return None if entry is None else {k: v for k, v in entry.items() if k != "token"}

    def delete(self, provider_id: str, name: str) -> bool:
        with interprocess_file_lock(self._lock_path):
            data = self._load()
            existed = data["entries"].pop(f"{provider_id}::{name}", None) is not None
            if existed:
                self._save(data)
            return existed


class RegistrySecurity:
    DEFAULT_POLICY = {"max_secret_age_days": 180, "forbid_shared_credential_refs": False,
                      "require_credential_present": False, "expiry_warning_days": 14}

    def __init__(self, core: RegistryCore, store: RegistryStore, authorization: ProviderAuthorizationManager | None = None,
                 vault: SecretVault | None = None, *, clock: Callable[[], datetime] | None = None) -> None:
        self.core, self.store, self.cm, self.bus = core, store, core.cm, core.bus
        self.auth, self.vault = authorization, vault
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with store.transaction() as data:
            data["ext"].setdefault("credential_refs", {})
            data["ext"].setdefault("security_policy", dict(self.DEFAULT_POLICY))
            data["ext"].setdefault("principal_scopes", {})
            data["ext"].setdefault("expiry", {})

    # ---- references (I01, I02, I05) ----
    def set_credential_ref(self, provider_id: str, env_var: str) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not isinstance(env_var, str) or not _ENV_NAME.fullmatch(env_var):
            return {"status": "INVALID_REFERENCE", "reason": "must be an UPPER_SNAKE environment variable name"}
        with self.store.transaction() as data:
            data["ext"]["credential_refs"][provider_id] = {"env": env_var}
        return {"status": "REFERENCE_SET", "provider": provider_id, "env": env_var}

    def credential_ref(self, provider_id: str) -> str | None:
        explicit = self.store.read()["ext"]["credential_refs"].get(provider_id, {}).get("env")
        if explicit:
            return explicit
        provider = self.cm.get_provider(provider_id) or {}
        return provider.get("token_env") or provider.get("api_key_env") or provider.get("credential_env")

    def shared_reference_report(self) -> dict[str, list[str]]:
        """I02: environment variables referenced by more than one provider (isolation violations)."""
        owners: dict[str, list[str]] = {}
        for provider in self.cm.list_providers():
            ref = self.credential_ref(provider["id"])
            if ref:
                owners.setdefault(ref, []).append(provider["id"])
        return {ref: sorted(pids) for ref, pids in owners.items() if len(pids) > 1}

    # ---- authorization / scope (I08, I14) ----
    def grant_scope(self, principal_id: str, provider_ids: list[str]) -> dict[str, Any]:
        unknown = [p for p in provider_ids if self.cm.get_provider(p) is None]
        if unknown:
            return {"status": "UNKNOWN_PROVIDERS", "providers": unknown}
        with self.store.transaction() as data:
            data["ext"]["principal_scopes"][principal_id] = sorted(set(provider_ids))
        return {"status": "SCOPE_GRANTED"}

    def _authorize(self, principal_id: str, provider_id: str, operation: str) -> dict[str, Any]:
        if self.auth is None:
            return {"allowed": False, "status": "NO_AUTHORIZATION_MANAGER"}  # fail closed
        verdict = self.auth.authorize(principal_id, operation)
        if not verdict.get("allowed"):
            return verdict
        scope = self.store.read()["ext"]["principal_scopes"].get(principal_id)
        if scope is not None and provider_id not in scope:
            return {"allowed": False, "status": "PROVIDER_OUT_OF_SCOPE"}
        return verdict

    # ---- audit (I13) ----
    def _audit(self, principal: str, provider_id: str, action: str, outcome: str, **info: Any) -> None:
        with self.store.transaction() as data:
            entry = {"ts": self.clock().isoformat(), "principal": principal, "provider": provider_id,
                     "action": action, "outcome": outcome, **{k: v for k, v in info.items() if isinstance(v, (str, int, float, bool, type(None)))}}
            data["audit"].append(entry)
            del data["audit"][:-AUDIT_LIMIT]
        self.bus.emit("security.credential_access", {"provider": provider_id, "action": action, "outcome": outcome})

    def audit_log(self, provider_id: str | None = None, action: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.store.read()["audit"]
                if (provider_id is None or e["provider"] == provider_id) and (action is None or e["action"] == action)]

    # ---- secure access (I03, I04, I05) ----
    def store_secret(self, principal_id: str, provider_id: str, name: str, value: str) -> dict[str, Any]:
        verdict = self._authorize(principal_id, provider_id, "provider.credentials.rotate")
        if not verdict.get("allowed"):
            self._audit(principal_id, provider_id, "store", "DENIED", reason=verdict.get("status"))
            return {"status": "ACCESS_DENIED", "reason": verdict.get("status")}
        if self.vault is None:
            return {"status": "VAULT_UNAVAILABLE"}
        result = self.vault.put(provider_id, name, value)
        self._audit(principal_id, provider_id, "store", result["status"], version=result.get("version"))
        return result

    def access_credential(self, principal_id: str, provider_id: str, *, purpose: str = "generate",
                          environ: Mapping[str, str] | None = None, vault_name: str = "default") -> SecretValue | dict[str, Any]:
        """Returns SecretValue on success, otherwise a status dict. Every call is audited (no values)."""
        verdict = self._authorize(principal_id, provider_id, "provider.generate")
        if not verdict.get("allowed"):
            self._audit(principal_id, provider_id, "access", "DENIED", reason=verdict.get("status"), purpose=purpose)
            return {"status": "ACCESS_DENIED", "reason": verdict.get("status")}
        if self.vault is not None:
            secret = self.vault.get(provider_id, vault_name)
            if secret is not None:
                self._audit(principal_id, provider_id, "access", "GRANTED", source="vault", purpose=purpose)
                return secret
        ref = self.credential_ref(provider_id)
        env = os.environ if environ is None else environ
        if ref and env.get(ref):
            self._audit(principal_id, provider_id, "access", "GRANTED", source="environment", purpose=purpose)
            return SecretValue(env[ref])
        self._audit(principal_id, provider_id, "access", "NOT_AVAILABLE", purpose=purpose)
        return {"status": "CREDENTIAL_NOT_AVAILABLE", "provider": provider_id}

    # ---- expiry / rotation (I06, I07) ----
    def set_expiry(self, provider_id: str, expires_at: datetime) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None or not isinstance(expires_at, datetime):
            return {"status": "INVALID"}
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        with self.store.transaction() as data:
            data["ext"]["expiry"][provider_id] = expires_at.astimezone(timezone.utc).isoformat()
        return {"status": "EXPIRY_SET"}

    def expiry_status(self, provider_id: str) -> dict[str, Any]:
        raw = self.store.read()["ext"]["expiry"].get(provider_id)
        if raw is None:
            return {"status": "UNKNOWN"}
        remaining = datetime.fromisoformat(raw) - self.clock()
        warn = self.store.read()["ext"]["security_policy"]["expiry_warning_days"]
        if remaining <= timedelta(0):
            return {"status": "EXPIRED", "expired_since_seconds": -remaining.total_seconds()}
        if remaining <= timedelta(days=warn):
            return {"status": "EXPIRING_SOON", "days_remaining": remaining.days}
        return {"status": "VALID", "days_remaining": remaining.days}

    def rotate_secret(self, principal_id: str, provider_id: str, new_value: str, name: str = "default") -> dict[str, Any]:
        """I07: replace the stored secret; the old ciphertext is overwritten and a new version recorded."""
        result = self.store_secret(principal_id, provider_id, name, new_value)
        if result.get("status") == "STORED":
            self.bus.emit("security.credential_rotated", {"provider": provider_id, "version": result["version"]})
            return {**result, "status": "ROTATED" if result["version"] > 1 else "STORED"}
        return result

    def secret_age_days(self, provider_id: str, name: str = "default") -> float | None:
        meta = self.vault.meta(provider_id, name) if self.vault else None
        if not meta:
            return None
        stamp = meta.get("rotated_at") or meta["created_at"]
        return (self.clock() - datetime.fromisoformat(stamp)).total_seconds() / 86400

    # ---- authentication validation (I09) ----
    def validate_authentication(self, provider_id: str, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        base = self.cm.credential_status(provider_id, dict(environ) if environ is not None else None)
        instance = self.cm.get_loaded_provider(provider_id)
        live = None
        auth_fn = getattr(instance, "authenticate", None) if instance is not None else None
        if callable(auth_fn):
            try:
                report = auth_fn()
                live = bool(isinstance(report, dict) and report.get("authenticated") is True)
            except Exception:
                live = False
        return {"provider": provider_id, "credential_status": base.get("status"), "credential_present": base.get("credential_present"),
                "live_authentication": live}

    # ---- TLS (I10, I11) ----
    @staticmethod
    def secure_ssl_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return context

    def validate_endpoint_tls(self, endpoint: str, *, allowed_hosts: set[str] | None = None) -> dict[str, Any]:
        """HTTPS only (loopback http refused here), no userinfo, host allow-list via the existing validator."""
        return ConnectionManager.validate_endpoint(endpoint, allowed_hosts=allowed_hosts, allow_loopback_http=False)

    def check_certificate(self, host: str, port: int = 443, *, context: ssl.SSLContext | None = None,
                          timeout: float = 5.0, warn_days: int | None = None) -> dict[str, Any]:
        """Perform a real TLS handshake with chain + hostname verification and report expiry."""
        context = context or self.secure_ssl_context()
        warn_days = warn_days if warn_days is not None else self.store.read()["ext"]["security_policy"]["expiry_warning_days"]
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    cert = tls.getpeercert()
                    version = tls.version()
        except ssl.SSLCertVerificationError as exc:
            return {"valid": False, "reason": "CERTIFICATE_VERIFICATION_FAILED", "detail": exc.verify_message}
        except ssl.SSLError as exc:
            return {"valid": False, "reason": "TLS_ERROR", "detail": type(exc).__name__}
        except OSError as exc:
            return {"valid": False, "reason": "CONNECTION_FAILED", "detail": type(exc).__name__}
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
        days = (expires - self.clock()).days
        return {"valid": True, "tls_version": version, "expires_at": expires.isoformat(), "days_remaining": days,
                "expiring_soon": days <= warn_days}

    # ---- leakage prevention (I12) ----
    @staticmethod
    def scan_for_secrets(value: Any) -> list[str]:
        return find_secrets(value)

    def assert_clean(self) -> dict[str, Any]:
        """Scan everything the registry persists (overlay + audit) for secret-looking values."""
        hits = find_secrets(self.store.read())
        return {"clean": not hits, "paths": hits}

    # ---- policy (I15) ----
    def set_policy(self, **changes: Any) -> dict[str, Any]:
        unknown = sorted(set(changes) - set(self.DEFAULT_POLICY))
        if unknown:
            return {"status": "UNKNOWN_POLICY_KEYS", "keys": unknown}
        with self.store.transaction() as data:
            data["ext"]["security_policy"].update(changes)
        return {"status": "POLICY_UPDATED"}

    def enforce(self, provider_id: str, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        policy = self.store.read()["ext"]["security_policy"]
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return {"compliant": False, "violations": ["NOT_FOUND"]}
        violations = []
        shared = self.shared_reference_report()
        ref = self.credential_ref(provider_id)
        if policy["forbid_shared_credential_refs"] and ref in shared:
            violations.append("SHARED_CREDENTIAL_REFERENCE")
        age = self.secret_age_days(provider_id)
        if age is not None and age > policy["max_secret_age_days"]:
            violations.append("SECRET_TOO_OLD")
        if self.expiry_status(provider_id)["status"] == "EXPIRED":
            violations.append("CREDENTIAL_EXPIRED")
        if policy["require_credential_present"] and str(provider.get("auth_type", "")).upper() not in {"NONE", ""}:
            status = self.cm.credential_status(provider_id, dict(environ) if environ is not None else None)
            if status.get("credential_present") is False and self.vault_has(provider_id) is False:
                violations.append("CREDENTIAL_MISSING")
        return {"compliant": not violations, "violations": violations}

    def vault_has(self, provider_id: str) -> bool:
        return bool(self.vault and self.vault.meta(provider_id, "default"))
