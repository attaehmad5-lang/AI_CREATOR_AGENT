"""Provider lifecycle + dependency management (section E).

Runtime instances stay owned by ConnectionManager (load/unload/enable/disable and
contract validation are reused). This layer adds ordering, state tracking,
dependency resolution, safe hot-reload and recovery.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .core import RegistryCore
from .storage import RegistryStore

STATES = ("REGISTERED", "LOADING", "LOADED", "ACTIVE", "INACTIVE", "UNLOADING", "UNLOADED", "DISABLED", "FAILED")
HISTORY_PER_PROVIDER = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LifecycleManager:
    def __init__(self, core: RegistryCore, store: RegistryStore, *, dependency_retries: int = 1) -> None:
        self.core, self.store, self.cm, self.bus = core, store, core.cm, core.bus
        self._factories: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._lock = threading.RLock()
        self._building: dict[str, threading.Event] = {}
        self.dependency_retries = dependency_retries
        with store.transaction() as data:
            data["ext"].setdefault("lifecycle", {})
            data["ext"].setdefault("conflicts", {})

    # ---- factories ----
    def register_factory(self, provider_id: str, factory: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not callable(factory):
            return {"status": "INVALID_FACTORY"}
        self._factories[provider_id] = factory
        return {"status": "FACTORY_REGISTERED", "provider": provider_id}

    # ---- state tracking (E10) ----
    def _set_state(self, provider_id: str, state: str, **info: Any) -> None:
        assert state in STATES
        with self.store.transaction() as data:
            record = data["ext"]["lifecycle"].setdefault(provider_id, {"state": "REGISTERED", "history": [], "failures": 0})
            record["history"].append({"from": record["state"], "to": state, "ts": _now(), **info})
            del record["history"][:-HISTORY_PER_PROVIDER]
            record["state"] = state
            if state == "FAILED":
                record["failures"] += 1
            elif state in {"ACTIVE", "LOADED"}:
                record["failures"] = 0
        self.bus.emit(f"provider.lifecycle.{state.lower()}", {"provider": provider_id, **info})

    def state(self, provider_id: str) -> str:
        if self.cm.get_provider(provider_id) is None:
            return "UNKNOWN"
        if not self.cm.is_provider_enabled(provider_id):
            return "DISABLED"
        record = self.store.read()["ext"]["lifecycle"].get(provider_id)
        return record["state"] if record else "REGISTERED"

    def history(self, provider_id: str) -> list[dict[str, Any]]:
        return list(self.store.read()["ext"]["lifecycle"].get(provider_id, {}).get("history", []))

    # ---- dependency graph (E11-E15) ----
    def dependencies(self, provider_id: str) -> list[str]:
        return list(self.store.read()["deps"].get(provider_id, []))

    def dependents(self, provider_id: str) -> list[str]:
        return sorted(p for p, deps in self.store.read()["deps"].items() if provider_id in deps)

    def _path(self, graph: dict[str, list[str]], start: str, goal: str) -> list[str] | None:
        stack, seen = [(start, [start])], set()
        while stack:
            node, path = stack.pop()
            if node == goal:
                return path
            if node in seen:
                continue
            seen.add(node)
            stack.extend((n, path + [n]) for n in graph.get(node, []))
        return None

    def add_dependency(self, provider_id: str, depends_on: str) -> dict[str, Any]:
        if provider_id == depends_on:
            return {"status": "CIRCULAR_DEPENDENCY", "cycle": [provider_id, provider_id]}
        for pid in (provider_id, depends_on):
            if self.cm.get_provider(pid) is None:
                return {"status": "NOT_FOUND", "provider": pid}
        with self.store.transaction() as data:
            graph = data["deps"]
            cycle = self._path(graph, depends_on, provider_id)  # would depends_on eventually need provider_id?
            if cycle is not None:
                return {"status": "CIRCULAR_DEPENDENCY", "cycle": [provider_id, *cycle]}
            deps = set(graph.get(provider_id, []))
            deps.add(depends_on)
            graph[provider_id] = sorted(deps)
        return {"status": "DEPENDENCY_ADDED", "provider": provider_id, "depends_on": depends_on}

    def remove_dependency(self, provider_id: str, depends_on: str) -> dict[str, Any]:
        with self.store.transaction() as data:
            if depends_on not in data["deps"].get(provider_id, []):
                return {"status": "NOT_A_DEPENDENCY"}
            data["deps"][provider_id].remove(depends_on)
        return {"status": "DEPENDENCY_REMOVED"}

    def find_cycles(self) -> list[list[str]]:
        """E14: detect cycles that bypassed add_dependency (e.g. a hand-edited or restored state)."""
        graph = self.store.read()["deps"]
        cycles, state = [], {}

        def visit(node: str, path: list[str]) -> None:
            state[node] = 1
            for nxt in graph.get(node, []):
                if state.get(nxt) == 1:
                    cycles.append(path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt])
                elif nxt not in state:
                    visit(nxt, path + [nxt])
            state[node] = 2
        for node in sorted(graph):
            if node not in state:
                visit(node, [node])
        return cycles

    def resolution_order(self, provider_id: str) -> dict[str, Any]:
        """E12: dependencies-first topological order including transitive deps (deterministic)."""
        graph = self.store.read()["deps"]
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        order: list[str] = []
        temp: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> bool:
            if node in done:
                return True
            if node in temp:
                return False
            temp.add(node)
            for dep in sorted(graph.get(node, [])):
                if not visit(dep):
                    return False
            temp.discard(node)
            done.add(node)
            order.append(node)
            return True
        if not visit(provider_id):
            return {"status": "CIRCULAR_DEPENDENCY", "cycles": self.find_cycles()}
        return {"status": "RESOLVED", "order": order}

    def declare_conflict(self, a: str, b: str) -> dict[str, Any]:
        """Two providers that must not be active together (E13)."""
        if a == b or self.cm.get_provider(a) is None or self.cm.get_provider(b) is None:
            return {"status": "INVALID_CONFLICT"}
        with self.store.transaction() as data:
            for x, y in ((a, b), (b, a)):
                data["ext"]["conflicts"][x] = sorted(set(data["ext"]["conflicts"].get(x, [])) | {y})
        return {"status": "CONFLICT_DECLARED"}

    def detect_conflicts(self, provider_id: str) -> list[dict[str, Any]]:
        """E13: missing/disabled/deprecated dependencies and mutually-exclusive providers that are active."""
        problems = []
        for dep in self.dependencies(provider_id):
            dep_provider = self.cm.get_provider(dep)
            if dep_provider is None:
                problems.append({"type": "MISSING_DEPENDENCY", "dependency": dep})
            elif dep_provider.get("enabled") is not True:
                problems.append({"type": "DISABLED_DEPENDENCY", "dependency": dep})
            elif self.core.is_deprecated(dep):
                problems.append({"type": "DEPRECATED_DEPENDENCY", "dependency": dep})
        for other in self.store.read()["ext"]["conflicts"].get(provider_id, []):
            if self.cm.get_loaded_provider(other) is not None:
                problems.append({"type": "MUTUALLY_EXCLUSIVE_ACTIVE", "with": other})
        return problems

    # ---- load / unload (E01, E02, E08, E09) ----
    def _initialize_instance(self, provider_id: str, instance: Any) -> tuple[bool, str | None]:
        hook = getattr(instance, "initialize", None)
        if callable(hook):
            try:
                hook()
            except Exception as exc:
                return False, type(exc).__name__
        return True, None

    def load(self, provider_id: str, factory: Callable[[dict[str, Any]], Any] | None = None, *,
             _stack: tuple[str, ...] = ()) -> dict[str, Any]:
        """Load one provider. The (possibly slow) factory + initialize() run OUTSIDE the registry lock so that
        different providers genuinely load in parallel; a per-provider in-flight marker keeps one build per provider."""
        with self._lock:
            provider = self.cm.get_provider(provider_id)
            if provider is None:
                return {"status": "NOT_FOUND", "provider": provider_id, "loaded": False}
            if self.core.is_retired(provider_id):
                return {"status": "PROVIDER_RETIRED", "provider": provider_id, "loaded": False}
            if provider_id in _stack:
                return {"status": "CIRCULAR_DEPENDENCY", "provider": provider_id, "loaded": False}
            if factory is not None:
                self.register_factory(provider_id, factory)
            if self.cm.get_loaded_provider(provider_id) is not None:
                return {"status": "ALREADY_LOADED", "provider": provider_id, "loaded": True}
            conflicts = [c for c in self.detect_conflicts(provider_id) if c["type"] == "MUTUALLY_EXCLUSIVE_ACTIVE"]
            if conflicts:
                return {"status": "DEPENDENCY_CONFLICT", "provider": provider_id, "loaded": False, "conflicts": conflicts}
            waiting = self._building.get(provider_id)
            if waiting is None:
                self._building[provider_id] = threading.Event()
        if waiting is not None:                                             # another thread is already building this provider
            waiting.wait(timeout=60)
            loaded = self.cm.get_loaded_provider(provider_id) is not None
            return {"status": "ALREADY_LOADED" if loaded else "LOAD_FAILED_IN_OTHER_THREAD", "provider": provider_id, "loaded": loaded}
        try:
            return self._load_owned(provider_id, provider, _stack)
        finally:
            with self._lock:
                self._building.pop(provider_id).set()

    def _load_owned(self, provider_id: str, provider: dict[str, Any], _stack: tuple[str, ...]) -> dict[str, Any]:
        newly_loaded: list[str] = []

        def rollback() -> None:
            for loaded_dep in reversed(newly_loaded):
                self._unload_one(loaded_dep)
        for dep in self.dependencies(provider_id):                          # dependencies first (E11, E15); recursion holds no lock
            if self.cm.get_loaded_provider(dep) is not None:
                continue
            result = {"status": "NO_FACTORY"}
            for _attempt in range(1 + self.dependency_retries):
                result = self.load(dep, _stack=_stack + (provider_id,))
                if result.get("loaded"):
                    break
            if not result.get("loaded"):
                rollback()
                self._set_state(provider_id, "FAILED", reason="DEPENDENCY_FAILED", dependency=dep)
                return {"status": "DEPENDENCY_FAILED", "provider": provider_id, "dependency": dep,
                        "dependency_status": result.get("status"), "loaded": False}
            if result["status"] == "LOADED":
                newly_loaded.append(dep)
        fac = self._factories.get(provider_id)
        if fac is None:
            return {"status": "NO_FACTORY", "provider": provider_id, "loaded": False}
        self._set_state(provider_id, "LOADING")
        try:                                                                 # slow part, outside the lock
            instance = fac(provider)
        except Exception as exc:
            self._set_state(provider_id, "FAILED", reason="FACTORY_RAISED", error_type=type(exc).__name__)
            rollback()
            return {"status": "PROVIDER_FACTORY_FAILED", "provider": provider_id, "error_type": type(exc).__name__, "loaded": False}
        contract = self.cm.validate_provider_contract(provider_id, instance) if instance is not None else {"valid": False}
        if not contract.get("valid"):
            self._close(instance)
            self._set_state(provider_id, "FAILED", reason="CONTRACT_VIOLATION")
            rollback()
            return {"status": "PROVIDER_CONTRACT_VIOLATION", "provider": provider_id, "loaded": False}
        ok, err = self._initialize_instance(provider_id, instance)           # E08, still outside the lock
        if not ok:
            self._close(instance)
            self._set_state(provider_id, "FAILED", reason="INITIALIZATION_FAILED", error_type=err)
            rollback()
            return {"status": "INITIALIZATION_FAILED", "provider": provider_id, "error_type": err, "loaded": False}
        with self._lock:                                                     # short atomic publish
            missing = [d for d in self.dependencies(provider_id) if self.cm.get_loaded_provider(d) is None]
            outcome = {"status": "DEPENDENCY_UNLOADED_DURING_LOAD", "dependency": missing[0]} if missing else \
                self.cm.load_provider(provider_id, lambda p, _i=instance: _i)
        if outcome.get("status") != "LOADED":
            self._close(instance)
            self._set_state(provider_id, "FAILED", reason=outcome.get("status"))
            rollback()
            return {**outcome, "provider": provider_id, "loaded": False}
        self._set_state(provider_id, "LOADED")
        self.core.metrics.incr("lifecycle.loaded")
        return {"status": "LOADED", "provider": provider_id, "loaded": True, "dependencies_loaded": newly_loaded}

    @staticmethod
    def _close(instance: Any) -> None:
        closer = getattr(instance, "close", None) or getattr(instance, "shutdown", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _unload_one(self, provider_id: str) -> dict[str, Any]:
        self._set_state(provider_id, "UNLOADING")
        result = self.cm.unload_provider(provider_id)
        if result.get("status") in {"UNLOADED", "NOT_LOADED"}:
            gone = self.cm.get_loaded_provider(provider_id) is None  # E09: verify shutdown really happened
            self._set_state(provider_id, "UNLOADED" if gone else "FAILED", verified=gone)
            return {**result, "verified_unloaded": gone}
        self._set_state(provider_id, "FAILED", reason=result.get("status"))
        return result

    def unload(self, provider_id: str, *, cascade: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.cm.get_provider(provider_id) is None:
                return {"status": "NOT_FOUND", "provider": provider_id}
            live_dependents = [d for d in self.dependents(provider_id) if self.cm.get_loaded_provider(d) is not None]
            if live_dependents and not cascade:
                return {"status": "HAS_LOADED_DEPENDENTS", "provider": provider_id, "dependents": live_dependents}
            unloaded = []
            for dependent in live_dependents:
                self.unload(dependent, cascade=True)
                unloaded.append(dependent)
            result = self._unload_one(provider_id)
            return {**result, "cascaded": unloaded}

    # ---- enable / disable / activate / deactivate (E03-E05) ----
    def enable(self, provider_id: str) -> dict[str, Any]:
        result = self.cm.set_provider_enabled(provider_id, True)
        if result.get("status") == "ENABLED":
            self._set_state(provider_id, "REGISTERED", reason="enabled")
        return result

    def disable(self, provider_id: str, *, cascade: bool = False) -> dict[str, Any]:
        live_dependents = [d for d in self.dependents(provider_id) if self.cm.get_loaded_provider(d) is not None]
        if live_dependents and not cascade:
            return {"status": "HAS_LOADED_DEPENDENTS", "provider": provider_id, "dependents": live_dependents}
        for dependent in live_dependents:
            self.unload(dependent, cascade=True)
        result = self.cm.set_provider_enabled(provider_id, False)
        if result.get("status") == "DISABLED":
            self._set_state(provider_id, "DISABLED")
        return result

    def activate(self, provider_id: str, factory: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if provider.get("enabled") is not True:
            enabled = self.enable(provider_id)
            if enabled.get("status") != "ENABLED":
                return enabled
        loaded = self.load(provider_id, factory)
        if not loaded.get("loaded"):
            return loaded
        self._set_state(provider_id, "ACTIVE")
        return {"status": "ACTIVE", "provider": provider_id}

    def deactivate(self, provider_id: str, *, cascade: bool = False) -> dict[str, Any]:
        result = self.unload(provider_id, cascade=cascade)
        if result.get("status") in {"UNLOADED", "NOT_LOADED"}:
            self._set_state(provider_id, "INACTIVE")
            return {"status": "INACTIVE", "provider": provider_id}
        return result

    # ---- restart / reload (E06, E07) ----
    def restart(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            fac = self._factories.get(provider_id)
            if fac is None:
                return {"status": "NO_FACTORY", "provider": provider_id}
            if self.cm.get_loaded_provider(provider_id) is not None:
                stopped = self.unload(provider_id)
                if stopped.get("status") not in {"UNLOADED", "NOT_LOADED"}:
                    return {"status": "RESTART_FAILED", "stage": "unload", "detail": stopped.get("status")}
            started = self.load(provider_id)
            return {"status": "RESTARTED" if started.get("loaded") else "RESTART_FAILED", "stage": "load",
                    "detail": started.get("status")}

    def reload(self, provider_id: str, factory: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        """E07: build + validate the replacement first (outside the lock); swap only if valid. Old instance survives any failure."""
        with self._lock:
            provider = self.cm.get_provider(provider_id)
            if provider is None:
                return {"status": "NOT_FOUND", "provider": provider_id}
            if self.cm.get_loaded_provider(provider_id) is None:
                return {"status": "NOT_LOADED", "provider": provider_id}
            fac = factory or self._factories.get(provider_id)
            if fac is None:
                return {"status": "NO_FACTORY", "provider": provider_id}
        try:
            new = fac(provider)
        except Exception as exc:
            return {"status": "RELOAD_REJECTED", "reason": "FACTORY_FAILED", "error_type": type(exc).__name__}
        if new is None or not self.cm.validate_provider_contract(provider_id, new)["valid"]:
            self._close(new)
            return {"status": "RELOAD_REJECTED", "reason": "CONTRACT_VIOLATION"}
        ok, err = self._initialize_instance(provider_id, new)
        if not ok:
            self._close(new)
            return {"status": "RELOAD_REJECTED", "reason": "INITIALIZATION_FAILED", "error_type": err}
        with self._lock:
            old = self.cm.get_loaded_provider(provider_id)
            if old is None:
                self._close(new)
                return {"status": "NOT_LOADED", "provider": provider_id}
            with self.cm._lock:
                self.cm._loaded_providers[provider_id] = new
            if factory is not None:
                self._factories[provider_id] = factory
        self._close(old)
        self.bus.emit("provider.reloaded", {"provider": provider_id})
        return {"status": "RELOADED", "provider": provider_id}

    # ---- dependency recovery (E15) ----
    def recover_dependencies(self, provider_id: str) -> dict[str, Any]:
        """Reload any dependency of a live provider that has dropped out of memory."""
        recovered, failed = [], []
        for dep in self.resolution_order(provider_id).get("order", [])[:-1]:
            if self.cm.get_loaded_provider(dep) is None:
                result = self.load(dep)
                (recovered if result.get("loaded") else failed).append(dep)
        return {"status": "RECOVERED" if not failed else "PARTIAL", "recovered": recovered, "failed": failed}
