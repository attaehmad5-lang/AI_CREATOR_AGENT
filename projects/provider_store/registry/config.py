"""Layered, versioned provider configuration (section D).

Resolution order (later wins):
  built-in schema defaults < global defaults < category defaults < active profile
  < provider-specific values < environment overrides.
Integrates with ConnectionManager.configure_provider (model/endpoint persistence)
and ProviderConfigLifecycleManager (schema migrations) instead of duplicating them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..provider_config_lifecycle import ProviderConfigLifecycleManager
from .core import RegistryCore
from .storage import RegistryStore

_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool, "list": list, "dict": dict}
DEFAULT_SCHEMA: dict[str, dict[str, Any]] = {
    "model": {"type": "str", "max_len": 200},
    "endpoint": {"type": "str", "max_len": 500},
    "timeout_seconds": {"type": "float", "min": 0.1, "max": 600, "default": 30},
    "max_retries": {"type": "int", "min": 0, "max": 10, "default": 2},
    "priority": {"type": "int", "min": 0, "max": 1000, "default": 100},
    "rate_limit_per_minute": {"type": "int", "min": 1, "max": 100000},
    "quality": {"type": "str", "enum": ["draft", "standard", "high", "ultra"], "default": "standard"},
    "region": {"type": "str", "max_len": 50},
}
ENV_PREFIX = "AICA_PROVIDER_"
MAX_VERSIONS = 50


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class ConfigManager:
    def __init__(self, core: RegistryCore, store: RegistryStore) -> None:
        self.core, self.store, self.cm, self.bus = core, store, core.cm, core.bus
        with store.transaction() as data:
            cfg = data["config"]
            cfg.setdefault("defaults", {})
            cfg.setdefault("categories", {})
            cfg.setdefault("providers", {})
            cfg.setdefault("profiles", {})
            cfg.setdefault("active_profile", None)
            cfg.setdefault("schema", copy.deepcopy(DEFAULT_SCHEMA))
            cfg.setdefault("version", 0)
            cfg.setdefault("versions", [])

    # ---- schema (D06) & validation (D05) ----
    def schema(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.store.read()["config"]["schema"])

    def register_schema_key(self, key: str, spec: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", str(key)) or spec.get("type") not in _TYPES:
            return {"status": "INVALID_SCHEMA"}
        if self.cm._contains_credential_value_field({key: 1}):
            return {"status": "SENSITIVE_KEY_REJECTED"}
        with self.store.transaction() as data:
            data["config"]["schema"][key] = copy.deepcopy(spec)
        return {"status": "SCHEMA_UPDATED", "key": key}

    def validate(self, values: Mapping[str, Any], schema: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        schema = schema if schema is not None else self.schema()
        errors: list[str] = []
        if not isinstance(values, Mapping):
            return {"valid": False, "errors": ["configuration must be an object"]}
        if self.cm._contains_credential_value_field(dict(values)):
            return {"valid": False, "errors": ["credential values are not allowed in configuration"]}
        for key, value in values.items():
            spec = schema.get(key)
            if spec is None:
                errors.append(f"unknown key: {key}")
                continue
            expected = _TYPES[spec["type"]]
            if isinstance(value, bool) and spec["type"] != "bool":
                errors.append(f"{key}: bool is not a valid {spec['type']}")
            elif not isinstance(value, expected):
                errors.append(f"{key}: expected {spec['type']}")
            else:
                if "min" in spec and value < spec["min"]:
                    errors.append(f"{key}: below minimum {spec['min']}")
                if "max" in spec and value > spec["max"]:
                    errors.append(f"{key}: above maximum {spec['max']}")
                if "enum" in spec and value not in spec["enum"]:
                    errors.append(f"{key}: must be one of {spec['enum']}")
                if "max_len" in spec and isinstance(value, str) and len(value) > spec["max_len"]:
                    errors.append(f"{key}: longer than {spec['max_len']}")
                if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                    errors.append(f"{key}: must be finite")
        return {"valid": not errors, "errors": errors}

    # ---- versioned mutation core (D09, D11, D14) ----
    def _commit(self, mutate, *, reason: str, expected_version: int | None = None) -> dict[str, Any]:
        with self.store.transaction() as data:
            cfg = data["config"]
            if expected_version is not None and cfg["version"] != expected_version:
                return {"status": "VERSION_CONFLICT", "expected": expected_version, "actual": cfg["version"]}
            before = self._layers(cfg)
            mutate(cfg)
            after = self._layers(cfg)
            if before == after:
                return {"status": "UNCHANGED", "version": cfg["version"]}
            cfg["version"] += 1
            prev_hash = cfg["versions"][-1]["hash"] if cfg["versions"] else ""
            entry = {"version": cfg["version"], "reason": reason, "layers": after, "prev": prev_hash}
            entry["hash"] = _hash({"version": entry["version"], "layers": after, "prev": prev_hash})
            cfg["versions"].append(entry)
            del cfg["versions"][:-MAX_VERSIONS]
            version = cfg["version"]
        self.bus.emit("config.changed", {"version": version, "reason": reason})
        return {"status": "APPLIED", "version": version}

    @staticmethod
    def _layers(cfg: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({k: cfg[k] for k in ("defaults", "categories", "providers", "profiles", "active_profile")})

    def version(self) -> int:
        return self.store.read()["config"]["version"]

    # ---- setters (D02, D03, D08) ----
    def set_defaults(self, values: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        check = self.validate(values)
        if not check["valid"]:
            return {"status": "INVALID_CONFIG", "errors": check["errors"]}
        return self._commit(lambda c: c["defaults"].update(copy.deepcopy(values)), reason="defaults", expected_version=expected_version)

    def set_category_defaults(self, category: str, values: dict[str, Any]) -> dict[str, Any]:
        check = self.validate(values)
        if not check["valid"]:
            return {"status": "INVALID_CONFIG", "errors": check["errors"]}
        return self._commit(lambda c: c["categories"].setdefault(str(category).casefold(), {}).update(copy.deepcopy(values)),
                            reason=f"category:{category}")

    def set_provider_config(self, provider_id: str, values: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        check = self.validate(values)
        if not check["valid"]:
            return {"status": "INVALID_CONFIG", "errors": check["errors"]}
        return self._commit(lambda c: c["providers"].setdefault(provider_id, {}).update(copy.deepcopy(values)),
                            reason=f"provider:{provider_id}", expected_version=expected_version)

    def create_profile(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,29}", str(name)):
            return {"status": "INVALID_PROFILE_NAME"}
        check = self.validate(values)
        if not check["valid"]:
            return {"status": "INVALID_CONFIG", "errors": check["errors"]}
        return self._commit(lambda c: c["profiles"].__setitem__(name, copy.deepcopy(values)), reason=f"profile:{name}")

    def activate_profile(self, name: str | None) -> dict[str, Any]:
        if name is not None and name not in self.store.read()["config"]["profiles"]:
            return {"status": "PROFILE_NOT_FOUND"}
        return self._commit(lambda c: c.__setitem__("active_profile", name), reason=f"activate:{name}")

    # ---- resolution (D01, D04, D07) ----
    def env_overrides(self, provider_id: str, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        """AICA_PROVIDER_<ID>__<KEY>=value ; only schema-known keys, values validated."""
        environ = os.environ if environ is None else environ
        prefix = f"{ENV_PREFIX}{re.sub(r'[^A-Za-z0-9]', '_', provider_id).upper()}__"
        schema = self.schema()
        found: dict[str, Any] = {}
        for name, raw in environ.items():
            if not name.startswith(prefix):
                continue
            key = name[len(prefix):].lower()
            spec = schema.get(key)
            if spec is None:
                continue
            try:
                value: Any = json.loads(raw) if spec["type"] != "str" else raw
            except ValueError:
                continue
            if self.validate({key: value})["valid"]:
                found[key] = value
        return found

    def effective(self, provider_id: str, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        cfg = self.store.read()["config"]
        merged = {k: v["default"] for k, v in cfg["schema"].items() if "default" in v}
        merged.update(cfg["defaults"])
        for category in self.core.categories_of(provider_id):
            merged.update(cfg["categories"].get(category, {}))
        if cfg["active_profile"]:
            merged.update(cfg["profiles"].get(cfg["active_profile"], {}))
        merged.update(cfg["providers"].get(provider_id, {}))
        merged.update(self.env_overrides(provider_id, environ))
        return {"status": "OK", "provider": provider_id, "config": merged, "version": cfg["version"]}

    def apply_to_connection_manager(self, provider_id: str) -> dict[str, Any]:
        """Persist model/endpoint through the existing ConnectionManager (host allow-list enforced there)."""
        resolved = self.effective(provider_id)
        if resolved["status"] != "OK":
            return resolved
        kwargs = {k: resolved["config"][k] for k in ("model", "endpoint") if k in resolved["config"]}
        if not kwargs:
            return {"status": "NOTHING_TO_APPLY", "provider": provider_id}
        return self.cm.configure_provider(provider_id, **kwargs)

    # ---- change detection (D10) ----
    def fingerprint(self) -> str:
        cfg = self.store.read()["config"]
        return _hash(self._layers(cfg))

    def has_changed_since(self, fingerprint: str) -> bool:
        return self.fingerprint() != fingerprint

    def diff(self, from_version: int, to_version: int | None = None) -> dict[str, Any]:
        cfg = self.store.read()["config"]
        by_v = {e["version"]: e["layers"] for e in cfg["versions"]}
        to_version = cfg["version"] if to_version is None else to_version
        if from_version not in by_v or to_version not in by_v:
            return {"status": "VERSION_NOT_RETAINED", "retained": sorted(by_v)}
        a, b = by_v[from_version], by_v[to_version]
        changes = {}
        for layer in a:
            if a[layer] != b[layer]:
                changes[layer] = {"from": a[layer], "to": b[layer]}
        return {"status": "OK", "changes": changes}

    # ---- rollback (D11) ----
    def rollback(self, to_version: int | None = None) -> dict[str, Any]:
        cfg = self.store.read()["config"]
        by_v = {e["version"]: e for e in cfg["versions"]}
        by_v[0] = {"layers": {"defaults": {}, "categories": {}, "providers": {}, "profiles": {}, "active_profile": None}}   # pristine state
        target = to_version if to_version is not None else cfg["version"] - 1
        if cfg["version"] == 0 or target not in by_v or target < 0:
            return {"status": "VERSION_NOT_RETAINED", "retained": sorted(by_v)}
        layers = by_v[target]["layers"]

        def mutate(c: dict[str, Any]) -> None:
            for key, value in layers.items():
                c[key] = copy.deepcopy(value)
        result = self._commit(mutate, reason=f"rollback_to:{target}")
        return {**result, "restored_version": target} if result["status"] == "APPLIED" else result

    # ---- import / export (D12) + conflict resolution (D14) ----
    def export_config(self, path: str | Path | None = None) -> dict[str, Any]:
        cfg = self.store.read()["config"]
        payload = {"format": "aica-registry-config/1", "layers": self._layers(cfg), "schema": cfg["schema"]}
        payload["sha256"] = _hash({"layers": payload["layers"], "schema": payload["schema"]})
        if path is not None:
            Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
        return payload

    def import_config(self, source: dict[str, Any] | str | Path, *, on_conflict: str = "fail") -> dict[str, Any]:
        if on_conflict not in {"fail", "keep_existing", "prefer_incoming"}:
            return {"status": "INVALID_POLICY"}
        if isinstance(source, (str, Path)):
            try:
                source = json.loads(Path(source).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"status": "UNREADABLE"}
        if not isinstance(source, dict) or source.get("format") != "aica-registry-config/1":
            return {"status": "INVALID_FORMAT"}
        if source.get("sha256") != _hash({"layers": source.get("layers"), "schema": source.get("schema")}):
            return {"status": "INTEGRITY_FAILED"}
        layers, schema = source["layers"], source["schema"]
        problems = []
        for scope in [layers.get("defaults", {}), *layers.get("categories", {}).values(), *layers.get("profiles", {}).values(),
                      *layers.get("providers", {}).values()]:
            check = self.validate(scope, schema)
            if not check["valid"]:
                problems.extend(check["errors"])
        if problems:
            return {"status": "INVALID_CONFIG", "errors": problems}
        unknown = [pid for pid in layers.get("providers", {}) if self.cm.get_provider(pid) is None]
        if unknown:
            return {"status": "UNKNOWN_PROVIDERS", "providers": unknown}
        current = self.store.read()["config"]
        conflicts = self._find_conflicts(current, layers)
        if conflicts and on_conflict == "fail":
            return {"status": "CONFLICTS", "conflicts": conflicts}

        def mutate(c: dict[str, Any]) -> None:
            def merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
                for key, value in src.items():
                    if key in dst and dst[key] != value and on_conflict == "keep_existing":
                        continue
                    dst[key] = copy.deepcopy(value)
            merge(c["defaults"], layers.get("defaults", {}))
            for scope in ("categories", "providers", "profiles"):
                for name, values in layers.get(scope, {}).items():
                    merge(c[scope].setdefault(name, {}), values)
        result = self._commit(mutate, reason="import")
        return {**result, "conflicts_resolved": len(conflicts), "policy": on_conflict}

    @staticmethod
    def _find_conflicts(current: dict[str, Any], incoming: dict[str, Any]) -> list[str]:
        out = []
        for key, value in incoming.get("defaults", {}).items():
            if key in current["defaults"] and current["defaults"][key] != value:
                out.append(f"defaults.{key}")
        for scope in ("categories", "providers", "profiles"):
            for name, values in incoming.get(scope, {}).items():
                for key, value in values.items():
                    if key in current[scope].get(name, {}) and current[scope][name][key] != value:
                        out.append(f"{scope}.{name}.{key}")
        return sorted(out)

    # ---- runtime updates (D13) ----
    def update_runtime(self, provider_id: str, values: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
        """Hot update: validate, version, notify; no restart. Rejected updates never change state."""
        outcome = self.set_provider_config(provider_id, values, expected_version=expected_version)
        if outcome["status"] == "APPLIED":
            self.bus.emit("provider.config_updated", {"provider": provider_id, "keys": sorted(values), "version": outcome["version"]})
        return outcome

    # ---- integrity (D15) ----
    def verify_integrity(self) -> dict[str, Any]:
        cfg = self.store.read()["config"]
        prev = ""
        for entry in cfg["versions"]:
            if entry["version"] != cfg["versions"][0]["version"] and entry["prev"] != prev:
                return {"valid": False, "broken_at": entry["version"], "reason": "CHAIN_BROKEN"}
            expected = _hash({"version": entry["version"], "layers": entry["layers"], "prev": entry["prev"]})
            if entry["hash"] != expected:
                return {"valid": False, "broken_at": entry["version"], "reason": "HASH_MISMATCH"}
            prev = entry["hash"]
        if cfg["versions"] and cfg["versions"][-1]["layers"] != self._layers(cfg):
            return {"valid": False, "broken_at": cfg["version"], "reason": "LIVE_STATE_DIVERGES_FROM_LAST_VERSION"}
        return {"valid": True, "versions_checked": len(cfg["versions"])}

    # ---- migration (integration with existing lifecycle manager) ----
    def migrate(self, config: dict[str, Any], *, target_version: str, migrations: dict[str, Any]) -> dict[str, Any]:
        manager = ProviderConfigLifecycleManager(lambda c: self.validate({k: v for k, v in c.items() if k != "schema_version"}))
        return manager.migrate(config, target_version=target_version, migrations=migrations)
