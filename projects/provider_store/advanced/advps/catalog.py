"""Provider/model catalog: registration, enable/disable, versioning, import/export, diff, rollback."""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from .errors import NotFoundError, ValidationError
from .security import validate_endpoint
from .storage import AtomicJsonStore, digest

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
SCHEMA_VERSION = 1
MAX_HISTORY = 20


def _empty() -> dict:
    return {"schema": SCHEMA_VERSION, "revision": 0, "providers": {}, "history": []}


def validate_provider(rec: dict, allowlist: Optional[Iterable[str]] = None) -> dict:
    if not isinstance(rec, dict):
        raise ValidationError("provider must be an object")
    pid = rec.get("id")
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        raise ValidationError(f"invalid provider id: {pid!r}")
    version = rec.get("version", "0.0.0")
    if not isinstance(version, str) or not re.match(r"^\d+\.\d+\.\d+$", version):
        raise ValidationError("version must be MAJOR.MINOR.PATCH")
    caps = rec.get("capabilities", [])
    if not isinstance(caps, list) or not all(isinstance(c, str) and c for c in caps):
        raise ValidationError("capabilities must be a list of non-empty strings")
    endpoint = rec.get("endpoint")
    if endpoint is not None:
        validate_endpoint(endpoint, allowlist=allowlist, resolve=False)
    models = rec.get("models", {})
    if not isinstance(models, dict):
        raise ValidationError("models must be an object")
    clean_models = {}
    for mid, m in models.items():
        if not isinstance(m, dict):
            raise ValidationError(f"model {mid!r} must be an object")
        price = m.get("price_per_unit")
        if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0):
            raise ValidationError(f"model {mid!r}: price_per_unit must be a non-negative number or null")
        mcaps = m.get("capabilities", caps)
        if not isinstance(mcaps, list):
            raise ValidationError(f"model {mid!r}: capabilities must be a list")
        clean_models[mid] = {
            "capabilities": list(mcaps),
            "price_per_unit": price,
            "unit": m.get("unit", "request"),
            "avg_latency_ms": m.get("avg_latency_ms"),
        }
    return {
        "id": pid,
        "name": str(rec.get("name", pid)),
        "version": version,
        "endpoint": endpoint,
        "enabled": bool(rec.get("enabled", True)),
        "capabilities": list(caps),
        "region": rec.get("region"),
        "priority": int(rec.get("priority", 100)),
        "models": clean_models,
        "metadata": dict(rec.get("metadata", {})),
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
        def op(state):
            if pid not in state["providers"]:
                raise NotFoundError(f"unknown provider: {pid}")
            self._commit(state, lambda s: s["providers"][pid].__setitem__("enabled", bool(enabled)))

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
                capability in m["capabilities"] for m in prov["models"].values()
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
        cleaned = [validate_provider(p, self.allowlist) for p in providers.values()]

        def op(state):
            def mutate(s):
                for p in cleaned:
                    if p["id"] in s["providers"] and not replace_existing:
                        continue
                    s["providers"][p["id"]] = p
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
