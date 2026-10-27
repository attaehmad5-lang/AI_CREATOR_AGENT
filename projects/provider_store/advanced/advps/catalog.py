"""Provider/model catalog: registration, enable/disable, versioning, import/export, diff, rollback."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Optional

from .errors import NotFoundError, ValidationError
from .security import validate_endpoint
from .storage import AtomicJsonStore, digest

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SCHEMA_VERSION = 1
MAX_HISTORY = 20
_SAFE_REFERENCE_KEYS = {"token_env", "token_expiry_env", "api_key_env", "secret_ref", "credential_env"}
_SECRET_MARKERS = ("access_token", "refresh_token", "api_key", "apikey", "password", "secret", "credential",
                   "authorization", "bearer", "private_key", "signing_key", "encryption_key", "cookie", "session")


def _empty() -> dict:
    return {"schema": SCHEMA_VERSION, "revision": 0, "providers": {}, "history": []}


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _SAFE_REFERENCE_KEYS:
                if not isinstance(item, str) or not item.strip():
                    return True
                continue
            if normalized == "token" or any(marker in normalized for marker in _SECRET_MARKERS):
                return True
            if _contains_secret_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValidationError(f"{field} must be a finite non-negative number")
    return number


def validate_provider(rec: dict, allowlist: Optional[Iterable[str]] = None) -> dict:
    if not isinstance(rec, dict):
        raise ValidationError("provider must be an object")
    pid = rec.get("id")
    if not isinstance(pid, str) or not _ID_RE.fullmatch(pid):
        raise ValidationError(f"invalid provider id: {pid!r}")
    version = rec.get("version", "0.0.0")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValidationError("version must be MAJOR.MINOR.PATCH")
    caps = rec.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) and c.strip() for c in caps):
        raise ValidationError("capabilities must be a list of non-empty strings")
    endpoint = rec.get("endpoint")
    if endpoint is not None:
        validate_endpoint(endpoint, allowlist=allowlist, resolve=False)
    enabled = rec.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError("enabled must be boolean")
    priority = rec.get("priority", 100)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValidationError("priority must be an integer")
    region = rec.get("region")
    if region is not None and (not isinstance(region, str) or not region.strip()):
        raise ValidationError("region must be a non-empty string or null")
    metadata = rec.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be an object")
    if _contains_secret_key(metadata):
        raise ValidationError("secret-bearing metadata is forbidden; use an environment reference")
    models = rec.get("models", {})
    if not isinstance(models, dict):
        raise ValidationError("models must be an object")
    clean_models = {}
    for mid, model in models.items():
        if not isinstance(mid, str) or not mid.strip() or not isinstance(model, dict):
            raise ValidationError(f"model {mid!r} must have a name and object metadata")
        price = model.get("price_per_unit")
        if price is not None:
            price = _finite_nonnegative(price, f"model {mid!r} price_per_unit")
        model_caps = model.get("capabilities", caps)
        if not isinstance(model_caps, list) or not all(isinstance(c, str) and c.strip() for c in model_caps):
            raise ValidationError(f"model {mid!r}: capabilities must be a list of non-empty strings")
        unit = model.get("unit", "request")
        if not isinstance(unit, str) or not unit.strip():
            raise ValidationError(f"model {mid!r}: unit must be a non-empty string")
        latency = model.get("avg_latency_ms")
        if latency is not None:
            latency = _finite_nonnegative(latency, f"model {mid!r} avg_latency_ms")
        clean_models[mid] = {
            "capabilities": list(model_caps),
            "price_per_unit": price,
            "unit": unit,
            "avg_latency_ms": latency,
        }
    return {
        "id": pid,
        "name": str(rec.get("name", pid)),
        "version": version,
        "endpoint": endpoint,
        "enabled": enabled,
        "capabilities": list(caps),
        "region": region,
        "priority": priority,
        "models": clean_models,
        "metadata": dict(metadata),
    }


class Catalog:
    def __init__(self, store: AtomicJsonStore, allowlist: Optional[Iterable[str]] = None, audit=None):
        self.store = store
        self.store.default = _empty
        self.allowlist = list(allowlist) if allowlist is not None else None
        self.audit = audit

    def _log(self, action: str, **kw: Any) -> None:
        if self.audit:
            self.audit.append(action, **kw)

    def _commit(self, state: dict, mutate) -> None:
        snapshot = {"revision": state["revision"], "providers": json.loads(json.dumps(state["providers"]))}
        mutate(state)
        state["history"].append(snapshot)
        state["history"] = state["history"][-MAX_HISTORY:]
        state["revision"] += 1

    def register(self, rec: dict, replace: bool = False) -> dict:
        clean = validate_provider(rec, self.allowlist)

        def op(state):
            if clean["id"] in state["providers"] and not replace:
                raise ValidationError(f"provider already registered: {clean['id']}")
            self._commit(state, lambda s: s["providers"].__setitem__(clean["id"], clean))

        self.store.update(lambda s: op(s))
        self._log("provider.register", id=clean["id"], version=clean["version"])
        return clean

    def unregister(self, pid: str) -> None:
        def op(state):
            if pid not in state["providers"]:
                raise NotFoundError(f"unknown provider: {pid}")
            self._commit(state, lambda s: s["providers"].pop(pid))
        self.store.update(op)
        self._log("provider.unregister", id=pid)

    def set_enabled(self, pid: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be boolean")
        def op(state):
            if pid not in state["providers"]:
                raise NotFoundError(f"unknown provider: {pid}")
            self._commit(state, lambda s: s["providers"][pid].__setitem__("enabled", enabled))
        self.store.update(op)
        self._log("provider.enable" if enabled else "provider.disable", id=pid)

    def get(self, pid: str) -> dict:
        prov = self.store.read()["providers"].get(pid)
        if prov is None:
            raise NotFoundError(f"unknown provider: {pid}")
        return prov

    def list(self, capability: Optional[str] = None, enabled_only: bool = False, region: Optional[str] = None) -> list:
        out = []
        for prov in self.store.read()["providers"].values():
            if enabled_only and not prov["enabled"]:
                continue
            if capability and capability not in prov["capabilities"] and not any(
                capability in model["capabilities"] for model in prov["models"].values()
            ):
                continue
            if region and prov.get("region") != region:
                continue
            out.append(prov)
        return sorted(out, key=lambda p: (p["priority"], p["id"]))

    @property
    def revision(self) -> int:
        return self.store.read()["revision"]

    def integrity_hash(self) -> str:
        return digest(self.store.read()["providers"])

    def export_json(self) -> str:
        state = self.store.read()
        return json.dumps(
            {"schema": SCHEMA_VERSION, "providers": state["providers"], "sha256": digest(state["providers"])},
            sort_keys=True, indent=2,
        )

    def import_json(self, text: str, replace_existing: bool = False) -> int:
        try:
            doc = json.loads(text)
            providers = doc["providers"]
            claimed = doc["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("malformed catalog export") from exc
        if claimed != digest(providers):
            raise ValidationError("catalog export checksum mismatch")
        if doc.get("schema") != SCHEMA_VERSION:
            raise ValidationError(f"unsupported schema: {doc.get('schema')!r}")
        if not isinstance(providers, dict):
            raise ValidationError("catalog providers must be an object")
        cleaned = [validate_provider(p, self.allowlist) for p in providers.values()]

        def op(state):
            def mutate(s):
                for provider in cleaned:
                    if provider["id"] in s["providers"] and not replace_existing:
                        continue
                    s["providers"][provider["id"]] = provider
            self._commit(state, mutate)
        self.store.update(op)
        self._log("catalog.import", count=len(cleaned))
        return len(cleaned)

    @staticmethod
    def diff(old: dict, new: dict) -> dict:
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
        return {"added": added, "removed": removed, "changed": changed}

    def diff_with_revision(self, revision: int) -> dict:
        state = self.store.read()
        for snap in state["history"]:
            if snap["revision"] == revision:
                return self.diff(snap["providers"], state["providers"])
        raise NotFoundError(f"revision not in history: {revision}")

    def rollback(self, revision: int) -> None:
        def op(state):
            for snap in state["history"]:
                if snap["revision"] == revision:
                    target = json.loads(json.dumps(snap["providers"]))
                    self._commit(state, lambda s: s.__setitem__("providers", target))
                    return
            raise NotFoundError(f"revision not in history: {revision}")
        self.store.update(op)
        self._log("catalog.rollback", revision=revision)
