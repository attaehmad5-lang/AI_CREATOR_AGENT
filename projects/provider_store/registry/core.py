"""Registry core: identity, registration, discovery, catalogs, tags/groups, search (sections A, B, C).

The provider catalog remains owned by ConnectionManager (single source of truth).
Registry-owned data (tags, groups, history, extra categories, descriptions) lives
in RegistryStore. Nothing here re-implements what ConnectionManager already does.
"""
from __future__ import annotations

import copy
import importlib.util
import inspect
import re
import sys
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from ..connection_manager import MODALITY_CONTRACTS, ConnectionManager
from .events import EventBus, MetricsCollector, StructuredLogger
from .storage import RegistryStore

REGISTRY_VERSION = "2.0.0"
HISTORY_LIMIT = 1000

# Catalog groups -> catalog categories they cover (C01-C10).
CATEGORY_GROUPS: dict[str, frozenset[str]] = {
    "image": frozenset({"image", "image_editing", "advanced_image_editing", "upscaling", "advanced_upscaling"}),
    "video": frozenset({"video", "video_editing", "advanced_video_editing"}),
    "voice": frozenset({"voice"}),
    "music": frozenset({"music"}),
    "text": frozenset({"text", "language_model", "script_story", "translation"}),
    "animation": frozenset({"animation", "lipsync"}),
    "audio_processing": frozenset({"audio", "advanced_audio", "dubbing", "transcription", "advanced_transcription"}),
    "document_generation": frozenset({"document_generation"}),
    "research": frozenset({"research"}),
}
EXTRA_CATEGORIES = frozenset({"animation", "document_generation", "research", "custom"})
KNOWN_CATEGORIES = frozenset(MODALITY_CONTRACTS) | EXTRA_CATEGORIES | frozenset().union(*CATEGORY_GROUPS.values())
_ID_CHARS = re.compile(r"[^a-z0-9]+")


class RegistryState(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


_TRANSITIONS = {
    RegistryState.CREATED: {RegistryState.INITIALIZING},
    RegistryState.INITIALIZING: {RegistryState.RUNNING, RegistryState.FAILED},
    RegistryState.RUNNING: {RegistryState.STOPPING},
    RegistryState.STOPPING: {RegistryState.STOPPED, RegistryState.FAILED},
    RegistryState.STOPPED: {RegistryState.INITIALIZING},
    RegistryState.FAILED: {RegistryState.INITIALIZING, RegistryState.STOPPED},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text).strip())
    if not match:
        raise ValueError(f"invalid version: {text!r}")
    return tuple(int(g or 0) for g in match.groups())  # type: ignore[return-value]


class BaseProviderMarker:  # placeholder so discovery can recognise subclasses without importing interface.py
    pass


class RegistryCore:
    def __init__(self, connection_manager: ConnectionManager, store: RegistryStore, *,
                 bus: EventBus | None = None) -> None:
        self.cm = connection_manager
        self.store = store
        self.bus = bus or EventBus()
        self.metrics: MetricsCollector = self.bus.metrics
        self.logger: StructuredLogger = self.bus.logger
        self._lock = threading.RLock()
        self._state = RegistryState.CREATED
        self.config: dict[str, Any] = {}
        self.version = REGISTRY_VERSION
        self.read_only = False  # disaster-recovery mode: all registry mutations are refused

    # ================= A. foundation =================
    # ---- state / lifecycle (A12, A13) ----
    @property
    def state(self) -> RegistryState:
        return self._state

    def _transition(self, new: RegistryState) -> None:
        with self._lock:
            if new not in _TRANSITIONS[self._state]:
                raise RuntimeError(f"illegal registry transition {self._state.value} -> {new.value}")
            old, self._state = self._state, new
        self.bus.emit("registry.state", {"from": old.value, "to": new.value})

    def initialize(self, config: dict[str, Any] | str | Path | None = None) -> dict[str, Any]:
        """A10: validate catalog + overlay, load config, move CREATED/STOPPED -> RUNNING."""
        self._transition(RegistryState.INITIALIZING)
        try:
            if config is not None:
                loaded = self.load_config(config)
                if loaded["status"] != "LOADED":
                    raise ValueError(f"config rejected: {loaded}")
            validation = self.cm.validate_catalog_schema(self.cm.catalog)
            if validation.get("valid") is not True:
                raise ValueError("catalog failed schema validation")
            integrity = self.store.detect_corruption()
            if integrity["corrupted"]:
                raise ValueError("registry state corrupted")
        except Exception as exc:
            self._transition(RegistryState.FAILED)
            return {"status": "FAILED", "error_type": type(exc).__name__, "reason": str(exc)}
        self._transition(RegistryState.RUNNING)
        return {"status": "RUNNING", "providers": len(self.cm.list_providers()), "version": self.version}

    def start(self) -> dict[str, Any]:
        return self.initialize()

    def stop(self) -> dict[str, Any]:
        """A14: orderly shutdown - unload every runtime instance, snapshot, mark STOPPED."""
        self._transition(RegistryState.STOPPING)
        unloaded, errors = [], []
        for provider in self.cm.list_providers():
            pid = provider.get("id")
            if self.cm.get_loaded_provider(pid) is not None:
                result = self.cm.unload_provider(pid)
                (unloaded if result.get("status") in {"UNLOADED", "NOT_LOADED"} else errors).append(pid)
        snap = self.store.create_snapshot("shutdown")
        self._transition(RegistryState.STOPPED if not errors else RegistryState.FAILED)
        return {"status": "STOPPED" if not errors else "STOPPED_WITH_ERRORS", "unloaded": unloaded,
                "errors": errors, "snapshot": snap["id"]}

    shutdown = stop

    def restart(self) -> dict[str, Any]:
        if self._state == RegistryState.RUNNING:
            self.stop()
        return self.initialize()

    # ---- config loader (A11) ----
    def load_config(self, source: dict[str, Any] | str | Path) -> dict[str, Any]:
        import json
        if isinstance(source, (str, Path)):
            try:
                source = json.loads(Path(source).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                return {"status": "UNREADABLE", "error_type": type(exc).__name__}
        if not isinstance(source, dict):
            return {"status": "INVALID_CONFIG", "reason": "config must be an object"}
        if self.cm._contains_credential_value_field(source):
            return {"status": "SENSITIVE_CONFIG_REJECTED"}
        allowed = {"history_limit", "reject_duplicate_names", "default_category", "max_bulk"}
        unknown = sorted(set(source) - allowed)
        if unknown:
            return {"status": "INVALID_CONFIG", "reason": "unknown keys", "unknown": unknown}
        self.config = {"history_limit": HISTORY_LIMIT, "reject_duplicate_names": True, "max_bulk": 500, **source}
        return {"status": "LOADED", "config": dict(self.config)}

    def _cfg(self, key: str, default: Any) -> Any:
        return self.config.get(key, default)

    # ---- version management (A15) ----
    def registry_version(self) -> dict[str, Any]:
        return {"registry": self.version, "catalog": str(self.cm.catalog.get("version", "unknown")),
                "overlay_schema": self.store.read().get("schema")}

    @staticmethod
    def compare_versions(a: str, b: str) -> int:
        pa, pb = parse_version(a), parse_version(b)
        return (pa > pb) - (pa < pb)

    def is_compatible(self, required: str) -> bool:
        """Compatible when same major and registry minor >= required minor."""
        req, cur = parse_version(required), parse_version(self.version)
        return req[0] == cur[0] and cur[1:] >= req[1:]

    # ---- identity (A04-A09) ----
    def generate_provider_id(self, name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name required")
        base = _ID_CHARS.sub("_", name.strip().casefold()).strip("_")[:90] or "provider"
        existing = {p.get("id") for p in self.cm.list_providers()}
        candidate, n = base, 2
        while candidate in existing:
            candidate = f"{base}_{n}"
            n += 1
        return candidate

    def identify(self, ref: str) -> dict[str, Any]:
        """Resolve an id or (case-insensitive) display name to exactly one provider."""
        if not isinstance(ref, str) or not ref.strip():
            return {"status": "INVALID_REFERENCE"}
        found = self.cm.get_provider(ref)
        if found is not None:
            return {"status": "FOUND", "provider_id": found["id"], "by": "id"}
        matches = [p for p in self.cm.list_providers() if str(p.get("name", "")).casefold() == ref.strip().casefold()]
        if len(matches) == 1:
            return {"status": "FOUND", "provider_id": matches[0]["id"], "by": "name"}
        if len(matches) > 1:
            return {"status": "AMBIGUOUS", "candidates": sorted(m["id"] for m in matches)}
        return {"status": "NOT_FOUND"}

    def _require(self, provider_id: str) -> dict[str, Any] | None:
        return self.cm.get_provider(provider_id) if isinstance(provider_id, str) else None

    def rename(self, provider_id: str, new_name: str) -> dict[str, Any]:
        if self.read_only:
            return {"status": "REGISTRY_READ_ONLY", "saved": False}
        current = self._require(provider_id)
        if current is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not isinstance(new_name, str) or not new_name.strip():
            return {"status": "INVALID_NAME"}
        clash = [p["id"] for p in self.cm.list_providers()
                 if p["id"] != provider_id and str(p.get("name", "")).casefold() == new_name.strip().casefold()]
        if clash:
            return {"status": "NAME_CONFLICT", "conflicts_with": clash}
        updated = dict(current, name=new_name.strip())
        result = self.cm.register_provider(updated, overwrite=True)
        if result.get("status") == "REGISTERED":
            self._record("rename", provider_id, previous=current)
        return result

    def update_metadata(self, provider_id: str, **fields: Any) -> dict[str, Any]:
        if self.read_only:
            return {"status": "REGISTRY_READ_ONLY", "saved": False}
        if self._require(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if self.cm._contains_credential_value_field(fields):
            return {"status": "SENSITIVE_METADATA_REJECTED"}
        with self.store.transaction() as data:
            meta = data["meta"].setdefault(provider_id, {})
            meta.update(copy.deepcopy(fields))
            meta["updated_at"] = _now()
        self.bus.emit("provider.metadata_updated", {"provider": provider_id, "fields": sorted(fields)})
        return {"status": "UPDATED", "provider": provider_id}

    def get_metadata(self, provider_id: str) -> dict[str, Any] | None:
        if self._require(provider_id) is None:
            return None
        return copy.deepcopy(self.store.view()["meta"].get(provider_id, {}))

    def set_description(self, provider_id: str, description: str) -> dict[str, Any]:
        if not isinstance(description, str) or len(description) > 2000:
            return {"status": "INVALID_DESCRIPTION"}
        return self.update_metadata(provider_id, description=description.strip())

    def set_category(self, provider_id: str, category: str) -> dict[str, Any]:
        if self.read_only:
            return {"status": "REGISTRY_READ_ONLY", "saved": False}
        current = self._require(provider_id)
        if current is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not isinstance(category, str) or category.strip().casefold() not in KNOWN_CATEGORIES:
            return {"status": "UNKNOWN_CATEGORY", "known": sorted(KNOWN_CATEGORIES)}
        result = self.cm.register_provider(dict(current, category=category.strip().casefold()), overwrite=True)
        if result.get("status") == "REGISTERED":
            self._record("set_category", provider_id, previous=current)
        return result

    # ================= B. registration & discovery =================
    def _record(self, action: str, provider_id: str, *, previous: dict[str, Any] | None = None,
                extra: dict[str, Any] | None = None) -> int:
        limit = int(self._cfg("history_limit", HISTORY_LIMIT))
        with self.store.transaction() as data:
            seq = 1 + max([h["seq"] for h in data["history"]] or [0])
            entry = {"seq": seq, "ts": _now(), "action": action, "provider": provider_id,
                     "previous": self.cm._sanitize_public_data(previous) if previous else None,
                     "undone": False, **(extra or {})}
            data["history"].append(entry)
            del data["history"][:-limit]
        return seq

    def registration_history(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        history = self.store.view()["history"]
        return [dict(h) for h in history if provider_id is None or h["provider"] == provider_id]

    def find_duplicates(self, provider: dict[str, Any]) -> list[dict[str, Any]]:
        """B10: same id, same display name, or same official API URL under another id."""
        pid = provider.get("id")
        name = str(provider.get("name", "")).strip().casefold()
        url = provider.get("official_api_url")
        out = []
        for existing in self.cm.list_providers():
            reasons = []
            if existing.get("id") == pid:
                reasons.append("ID")
            elif name and str(existing.get("name", "")).strip().casefold() == name:
                reasons.append("NAME")
            elif url and existing.get("official_api_url") == url:
                reasons.append("API_URL")
            if reasons:
                out.append({"provider": existing["id"], "reasons": reasons})
        return out

    def detect_id_conflict(self, provider_id: str) -> bool:
        return self.cm.provider_exists(provider_id)

    def validate_registration(self, provider: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        if not isinstance(provider, dict):
            return {"valid": False, "errors": ["provider must be an object"]}
        for key in ("name", "category", "auth_type", "connection_type"):
            if not isinstance(provider.get(key), str) or not provider[key].strip():
                errors.append(f"missing or empty: {key}")
        if isinstance(provider.get("category"), str) and provider["category"].strip().casefold() not in KNOWN_CATEGORIES:
            errors.append(f"unknown category: {provider['category']}")
        pid = provider.get("id")
        if pid is not None and (not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pid)):
            errors.append("invalid id")
        if self.cm._contains_credential_value_field(provider):
            errors.append("credential value fields are not allowed in provider metadata")
        return {"valid": not errors, "errors": errors}

    def register(self, provider: dict[str, Any], *, overwrite: bool = False) -> dict[str, Any]:
        """B1/B2: validated registration; ID generated when absent; duplicates/conflicts reported."""
        if self.read_only:
            return {"status": "REGISTRY_READ_ONLY", "saved": False}
        verdict = self.validate_registration(provider)
        if not verdict["valid"]:
            return {"status": "INVALID_PROVIDER", "errors": verdict["errors"]}
        candidate = dict(provider)
        if not candidate.get("id"):
            candidate["id"] = self.generate_provider_id(candidate["name"])
        candidate["category"] = candidate["category"].strip().casefold()
        candidate.setdefault("enabled", True)
        dups = [d for d in self.find_duplicates(candidate) if d["provider"] != candidate["id"] or not overwrite]
        id_conflict = any("ID" in d["reasons"] for d in dups)
        name_dups = [d for d in dups if "ID" not in d["reasons"]]
        if id_conflict and not overwrite:
            return {"status": "ALREADY_EXISTS", "provider": candidate["id"], "saved": False, "duplicates": dups}
        if name_dups and self._cfg("reject_duplicate_names", True):
            return {"status": "DUPLICATE_PROVIDER", "provider": candidate["id"], "saved": False, "duplicates": name_dups}
        previous = self.cm.get_provider(candidate["id"])
        result = self.cm.register_provider(candidate, overwrite=overwrite)
        if result.get("status") == "REGISTERED":
            seq = self._record("register", candidate["id"], previous=previous)
            with self.store.transaction() as data:
                data["meta"].setdefault(candidate["id"], {}).setdefault("created_at", _now())
            self.bus.emit("provider.registered", {"provider": candidate["id"], "overwritten": previous is not None})
            self.metrics.incr("registry.registered")
            result = dict(result, history_seq=seq)
        return result

    def rollback_registration(self, provider_id: str) -> dict[str, Any]:
        """B13: undo the most recent not-yet-undone registration/rename/category change for one provider."""
        if self.read_only:
            return {"status": "REGISTRY_READ_ONLY", "saved": False}
        with self.store.transaction() as data:
            entries = [h for h in data["history"] if h["provider"] == provider_id and not h["undone"]
                       and h["action"] in {"register", "rename", "set_category"}]
            if not entries:
                return {"status": "NOTHING_TO_ROLLBACK", "provider": provider_id}
            entry = entries[-1]
            entry["undone"] = True
        previous = entry.get("previous")
        if previous is None:
            removed = self._remove_from_catalog(provider_id)
            self._record("rollback", provider_id, previous=None, extra={"of": entry["seq"], "removed": removed})
            self.bus.emit("provider.registration_rolled_back", {"provider": provider_id})
            return {"status": "ROLLED_BACK", "provider": provider_id, "removed": removed}
        restored = self.cm.register_provider(previous, overwrite=True)
        self._record("rollback", provider_id, previous=None, extra={"of": entry["seq"]})
        self.bus.emit("provider.registration_rolled_back", {"provider": provider_id})
        return {"status": "ROLLED_BACK" if restored.get("status") == "REGISTERED" else "ROLLBACK_FAILED",
                "provider": provider_id}

    def _remove_from_catalog(self, provider_id: str) -> bool:
        with self.cm._lock, self.cm._catalog_transaction():
            providers = self.cm._list_providers_unlocked()
            kept = [p for p in providers if p.get("id") != provider_id]
            if len(kept) == len(providers):
                return False
            new_catalog = dict(self.cm.catalog, providers=kept)
            if self.cm.validate_catalog_schema(new_catalog).get("valid") is not True:
                return False
            if self.cm.get_loaded_provider(provider_id) is not None:
                self.cm._loaded_providers.pop(provider_id, None)
            self.cm._atomic_write_json(self.cm.catalog_path, new_catalog)
            self.cm.catalog = new_catalog
            return True

    def register_many(self, providers: Iterable[dict[str, Any]], *, atomic: bool = True) -> dict[str, Any]:
        """B14: bulk registration. atomic=True validates everything first and rolls back on any failure."""
        items = list(providers)
        if len(items) > int(self._cfg("max_bulk", 500)):
            return {"status": "TOO_MANY", "limit": self._cfg("max_bulk", 500)}
        ids_seen: set[str] = set()
        pre_errors = {}
        for index, item in enumerate(items):
            verdict = self.validate_registration(item)
            if not verdict["valid"]:
                pre_errors[index] = verdict["errors"]
            elif item.get("id") in ids_seen:
                pre_errors[index] = ["duplicate id within batch"]
            elif item.get("id"):
                ids_seen.add(item["id"])
        if atomic and pre_errors:
            return {"status": "REJECTED", "registered": [], "errors": pre_errors}
        done: list[str] = []
        failures: dict[int, Any] = dict(pre_errors)
        for index, item in enumerate(items):
            if index in pre_errors:
                continue
            outcome = self.register(item)
            if outcome.get("status") == "REGISTERED":
                done.append(outcome["provider"])
            else:
                failures[index] = outcome
                if atomic:
                    for pid in reversed(done):
                        self.rollback_registration(pid)
                    return {"status": "ROLLED_BACK", "registered": [], "errors": failures}
        return {"status": "REGISTERED" if not failures else "PARTIAL", "registered": done, "errors": failures}

    # ---- discovery & detection (B3-B9) ----
    def discover(self, **filters: Any) -> list[dict[str, Any]]:
        """B3: dynamic discovery over the live catalog."""
        return self.cm.discover_providers(**filters)

    @staticmethod
    def import_module_from_path(path: str | Path) -> dict[str, Any]:
        """B5: import one provider module file under an isolated name; failures are returned, not raised."""
        path = Path(path)
        if not path.is_file() or path.suffix != ".py":
            return {"status": "NOT_A_MODULE", "path": str(path)}
        name = f"_registry_import_{abs(hash(str(path.resolve())))}"
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as exc:
            sys.modules.pop(name, None)
            return {"status": "IMPORT_FAILED", "path": str(path), "error_type": type(exc).__name__}
        return {"status": "IMPORTED", "module": module, "path": str(path)}

    def discover_modules(self, root: str | Path) -> list[Path]:
        """B6: candidate provider module files (``*_provider.py`` or any file defining PROVIDER_META)."""
        root = Path(root)
        if not root.is_dir():
            return []
        found = []
        for path in sorted(root.rglob("*.py")):
            if path.name.startswith("_") or "test" in path.parts:
                continue
            if path.name.endswith("_provider.py") or "PROVIDER_META" in path.read_text(encoding="utf-8", errors="ignore"):
                found.append(path)
        return found

    def discover_packages(self, root: str | Path) -> list[Path]:
        """B7: directories that look like provider packages (__init__.py + provider module/manifest)."""
        root = Path(root)
        if not root.is_dir():
            return []
        return sorted(d for d in root.iterdir() if d.is_dir() and (d / "__init__.py").is_file()
                      and (self.discover_modules(d) or (d / "provider.json").is_file()))

    def discover_classes(self, module: Any) -> list[type]:
        """B8: classes defined in a module that declare PROVIDER_META."""
        return [cls for _n, cls in inspect.getmembers(module, inspect.isclass)
                if cls.__module__ == getattr(module, "__name__", None) and isinstance(getattr(cls, "PROVIDER_META", None), dict)]

    @staticmethod
    def detect_interface(cls: type) -> dict[str, Any]:
        """B9: which modality contracts does this class satisfy (callable methods present)?"""
        satisfied = sorted(
            modality for modality, requirements in MODALITY_CONTRACTS.items()
            if all(any(callable(getattr(cls, name, None)) for name in alternatives) for alternatives in requirements))
        return {"class": cls.__name__, "modalities": satisfied, "implements_contract": bool(satisfied)}

    def detect_providers(self, root: str | Path) -> list[dict[str, Any]]:
        """B4: scan an explicit directory (never sys.path) for provider classes and their interface."""
        detected = []
        for path in self.discover_modules(root):
            imported = self.import_module_from_path(path)
            if imported["status"] != "IMPORTED":
                detected.append({"path": str(path), "status": imported["status"]})
                continue
            for cls in self.discover_classes(imported["module"]):
                info = self.detect_interface(cls)
                detected.append({"path": str(path), "status": "DETECTED", "meta": dict(cls.PROVIDER_META), **info})
        return detected

    def auto_register(self, root: str | Path) -> dict[str, Any]:
        """B1: detect + register every valid provider class found under root (rejects unsafe/duplicate ones)."""
        results = {"registered": [], "skipped": []}
        for item in self.detect_providers(root):
            if item.get("status") != "DETECTED":
                results["skipped"].append({"path": item["path"], "reason": item["status"]})
                continue
            if not item["implements_contract"]:
                results["skipped"].append({"path": item["path"], "reason": "NO_CONTRACT", "class": item["class"]})
                continue
            outcome = self.register(dict(item["meta"]))
            (results["registered"] if outcome.get("status") == "REGISTERED" else results["skipped"]).append(
                outcome.get("provider") if outcome.get("status") == "REGISTERED"
                else {"class": item["class"], "reason": outcome.get("status")})
        return results

    # ================= C. catalog & organization =================
    def _extra_categories(self, provider_id: str, view: dict[str, Any] | None = None) -> set[str]:
        meta = (view if view is not None else self.store.view())["meta"].get(provider_id, {})
        return set(meta.get("extra_categories", []))

    def add_category(self, provider_id: str, category: str) -> dict[str, Any]:
        """C11: multi-category support (primary category stays in the catalog)."""
        if self._require(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not isinstance(category, str) or category.casefold() not in KNOWN_CATEGORIES:
            return {"status": "UNKNOWN_CATEGORY"}
        with self.store.transaction() as data:
            extra = set(data["meta"].setdefault(provider_id, {}).get("extra_categories", []))
            extra.add(category.casefold())
            data["meta"][provider_id]["extra_categories"] = sorted(extra)
        return {"status": "UPDATED", "provider": provider_id, "categories": sorted(self.categories_of(provider_id))}

    def categories_of(self, provider_id: str, *, _provider: dict[str, Any] | None = None,
                      _view: dict[str, Any] | None = None) -> list[str]:
        provider = _provider if _provider is not None else self._require(provider_id)
        if provider is None:
            return []
        return sorted({str(provider.get("category", "")).casefold(), *self._extra_categories(provider_id, _view)} - {""})

    def catalog_for(self, group: str, *, _providers: list[dict[str, Any]] | None = None,
                    _view: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """C1-C10: providers whose primary or extra categories belong to the named catalog group."""
        group = str(group).casefold()
        providers = _providers if _providers is not None else self.cm.list_providers()
        view = _view if _view is not None else self.store.view()
        cats = {p["id"]: set(self.categories_of(p["id"], _provider=p, _view=view)) for p in providers}
        if group == "custom":
            known = frozenset().union(*CATEGORY_GROUPS.values())
            return [p for p in providers if "custom" in cats[p["id"]] or not (cats[p["id"]] & known)]
        members = CATEGORY_GROUPS.get(group)
        if members is None:
            return []
        return [p for p in providers if cats[p["id"]] & members]

    def catalog_overview(self) -> dict[str, int]:
        providers, view = self.cm.list_providers(), self.store.view()
        return {group: len(self.catalog_for(group, _providers=providers, _view=view)) for group in [*CATEGORY_GROUPS, "custom"]}

    def is_deprecated(self, provider_id: str) -> bool:
        provider = self.cm.get_provider(provider_id) or {}
        return bool(provider.get("deprecated") is True or self.store.view()["meta"].get(provider_id, {}).get("deprecation"))

    def is_retired(self, provider_id: str) -> bool:
        return bool(self.store.view()["meta"].get(provider_id, {}).get("retired_at"))

    # ---- tags / groups (C12, C13) ----
    @staticmethod
    def _clean_label(label: str) -> str | None:
        label = str(label).strip().casefold()
        return label if re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,49}", label) else None

    def add_tag(self, provider_id: str, tag: str) -> dict[str, Any]:
        clean = self._clean_label(tag)
        if self._require(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if clean is None:
            return {"status": "INVALID_TAG"}
        with self.store.transaction() as data:
            tags = set(data["tags"].get(provider_id, []))
            tags.add(clean)
            data["tags"][provider_id] = sorted(tags)
        return {"status": "TAGGED", "provider": provider_id, "tags": self.tags(provider_id)}

    def remove_tag(self, provider_id: str, tag: str) -> dict[str, Any]:
        clean = self._clean_label(tag)
        with self.store.transaction() as data:
            tags = set(data["tags"].get(provider_id, []))
            if clean not in tags:
                return {"status": "NOT_TAGGED"}
            tags.discard(clean)
            data["tags"][provider_id] = sorted(tags)
        return {"status": "UNTAGGED", "tags": self.tags(provider_id)}

    def tags(self, provider_id: str) -> list[str]:
        return list(self.store.view()["tags"].get(provider_id, []))

    def find_by_tag(self, tag: str) -> list[dict[str, Any]]:
        clean = self._clean_label(tag)
        tagged = {pid for pid, tags in self.store.view()["tags"].items() if clean in tags}
        return [p for p in self.cm.list_providers() if p["id"] in tagged]

    def create_group(self, group: str) -> dict[str, Any]:
        clean = self._clean_label(group)
        if clean is None:
            return {"status": "INVALID_GROUP"}
        with self.store.transaction() as data:
            if clean in data["groups"]:
                return {"status": "ALREADY_EXISTS", "group": clean}
            data["groups"][clean] = []
        return {"status": "CREATED", "group": clean}

    def add_to_group(self, group: str, provider_id: str) -> dict[str, Any]:
        clean = self._clean_label(group)
        if self._require(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        with self.store.transaction() as data:
            if clean not in data["groups"]:
                return {"status": "GROUP_NOT_FOUND", "group": group}
            members = set(data["groups"][clean])
            members.add(provider_id)
            data["groups"][clean] = sorted(members)
        return {"status": "ADDED", "group": clean, "members": self.group_members(clean)}

    def remove_from_group(self, group: str, provider_id: str) -> dict[str, Any]:
        clean = self._clean_label(group)
        with self.store.transaction() as data:
            if clean not in data["groups"]:
                return {"status": "GROUP_NOT_FOUND"}
            if provider_id not in data["groups"][clean]:
                return {"status": "NOT_A_MEMBER"}
            data["groups"][clean].remove(provider_id)
        return {"status": "REMOVED"}

    def group_members(self, group: str) -> list[str]:
        return list(self.store.view()["groups"].get(self._clean_label(group) or "", []))

    def list_groups(self) -> dict[str, list[str]]:
        return copy.deepcopy(self.store.view()["groups"])

    # ---- classification & search (C14, C15) ----
    def classify(self, provider_id: str) -> dict[str, Any]:
        provider = self._require(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND"}
        categories = set(self.categories_of(provider_id, _provider=provider))
        groups = sorted(g for g, members in CATEGORY_GROUPS.items() if categories & members)
        traits = []
        auth = str(provider.get("auth_type", "")).upper()
        conn = str(provider.get("connection_type", "")).upper()
        traits.append("local" if "LOCAL" in conn or "LOCAL" in auth else "remote")
        traits.append("keyless" if auth == "NONE" else "credentialed")
        traits.append("paid_required" if provider.get("subscription_required") is True else "no_subscription_required")
        if provider.get("zero_cost_allowed") is True:
            traits.append("zero_cost")
        if self.is_deprecated(provider_id):
            traits.append("deprecated")
        return {"status": "CLASSIFIED", "provider": provider_id, "groups": groups or ["custom"], "traits": traits}

    def search(self, query: str = "", *, category: str | None = None, capability: str | None = None,
               tag: str | None = None, enabled: bool | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """C15: ranked text search (id > name > description/tags/capabilities) with structured filters."""
        terms = [t for t in re.split(r"\s+", str(query).casefold().strip()) if t]
        snapshot = self.store.view()
        results = []
        for provider in self.cm.list_providers():
            pid = provider["id"]
            if category and category.casefold() not in self.categories_of(pid, _provider=provider, _view=snapshot):
                continue
            if capability and capability.casefold() not in {str(c).casefold() for c in provider.get("capabilities", [])}:
                continue
            if tag and self._clean_label(tag) not in snapshot["tags"].get(pid, []):
                continue
            if enabled is not None and provider.get("enabled") is not enabled:
                continue
            score = 0
            hay_name = str(provider.get("name", "")).casefold()
            hay_other = " ".join([str(snapshot["meta"].get(pid, {}).get("description", "")),
                                  " ".join(snapshot["tags"].get(pid, [])),
                                  " ".join(str(c) for c in provider.get("capabilities", []))]).casefold()
            for term in terms:
                if term in pid.casefold():
                    score += 10
                if term in hay_name:
                    score += 6
                if term in hay_other:
                    score += 2
            if terms and score == 0:
                continue
            results.append((score, pid, provider))
        results.sort(key=lambda r: (-r[0], r[1]))
        return [dict(p) for _s, _i, p in results[: max(1, int(limit))]]
