"""Performance, concurrency and multi-worker support (section K)."""
from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from ..interprocess_lock import interprocess_file_lock
from .core import RegistryCore
from .lifecycle import LifecycleManager
from .storage import RegistryStore


# ---- K04/K01: async facade ----------------------------------------------------
class AsyncRegistry:
    """Async API over any object exposing the synchronous registry methods (threads, not a re-implementation)."""

    def __init__(self, core: RegistryCore, *, router: Any = None, interface: Any = None, max_workers: int = 8) -> None:
        self.core, self.router, self.interface = core, router, interface
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="registry-async")

    async def _run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: func(*args, **kwargs))

    async def register(self, provider: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._run(self.core.register, provider, **kw)

    async def register_many_concurrently(self, providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.register(p) for p in providers)))

    async def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return await self._run(self.core.cm.get_provider, provider_id)

    async def search(self, query: str = "", **kw: Any) -> list[dict[str, Any]]:
        return await self._run(self.core.search, query, **kw)

    async def select(self, **kw: Any) -> dict[str, Any]:
        return await self._run(self.router.select, **kw)

    async def invoke(self, provider_id: str, request: Any) -> dict[str, Any]:
        return await self._run(self.interface.invoke, provider_id, request)

    def close(self) -> None:
        self._pool.shutdown(wait=True)


# ---- K05/K06: cache with generation + mtime invalidation ----------------------
class RegistryCache:
    def __init__(self, core: RegistryCore, store: RegistryStore, *, ttl_seconds: float = 30.0, max_entries: int = 256,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.core, self.store, self.clock = core, store, clock
        self.ttl, self.max_entries = ttl_seconds, max_entries
        self._entries: OrderedDict[str, tuple[float, tuple[int, int], Any]] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = self.misses = self.invalidations = 0
        core.bus.subscribe("provider.*", lambda _e: self.invalidate())
        core.bus.subscribe("config.*", lambda _e: self.invalidate())

    def _stamp(self) -> tuple[int, int]:
        try:
            catalog = self.core.cm.catalog_path.stat().st_mtime_ns
        except OSError:
            catalog = -1
        return (self.store.peek_generation(), catalog)

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> Any:
        now, stamp = self.clock(), self._stamp()
        with self._lock:
            hit = self._entries.get(key)
            if hit and now - hit[0] <= self.ttl and hit[1] == stamp:
                self._entries.move_to_end(key)
                self.hits += 1
                return hit[2]
            if hit:
                self.invalidations += 1
                del self._entries[key]
        value = compute()
        with self._lock:
            self.misses += 1
            self._entries[key] = (now, self._stamp(), value)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return value

    def invalidate(self, prefix: str = "") -> int:
        with self._lock:
            keys = [k for k in self._entries if k.startswith(prefix)]
            for k in keys:
                del self._entries[k]
            self.invalidations += len(keys)
            return len(keys)

    # cached lookups
    def providers(self) -> list[dict[str, Any]]:
        return self.get_or_compute("providers", self.core.cm.list_providers)

    def search(self, query: str, **kw: Any) -> list[dict[str, Any]]:
        key = "search:" + query + ":" + repr(sorted(kw.items()))
        return self.get_or_compute(key, lambda: self.core.search(query, **kw))

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "invalidations": self.invalidations, "entries": len(self._entries)}


# ---- K14: index ------------------------------------------------------------------
class LookupIndex:
    """O(1) id/category/capability lookups; rebuilt only when the underlying state changed."""

    def __init__(self, core: RegistryCore, store: RegistryStore) -> None:
        self.core, self.store = core, store
        self._stamp: tuple[int, int] | None = None
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_category: dict[str, list[str]] = {}
        self._by_capability: dict[str, list[str]] = {}
        self.rebuilds = 0
        self._lock = threading.RLock()

    def _current(self) -> tuple[int, int]:
        try:
            m = self.core.cm.catalog_path.stat().st_mtime_ns
        except OSError:
            m = -1
        return (self.store.peek_generation(), m)

    def refresh(self) -> bool:
        stamp = self._current()
        with self._lock:
            if stamp == self._stamp:
                return False
            providers = self.core.cm.list_providers()
            self._by_id = {p["id"]: p for p in providers}
            self._by_category, self._by_capability = {}, {}
            for p in providers:
                for cat in self.core.categories_of(p["id"]):
                    self._by_category.setdefault(cat, []).append(p["id"])
                for cap in p.get("capabilities", []):
                    self._by_capability.setdefault(str(cap), []).append(p["id"])
            self._stamp = stamp
            self.rebuilds += 1
            return True

    def get(self, provider_id: str) -> dict[str, Any] | None:
        self.refresh()
        return self._by_id.get(provider_id)

    def ids_for_category(self, category: str) -> list[str]:
        self.refresh()
        return list(self._by_category.get(category.casefold(), []))

    def ids_for_capability(self, capability: str) -> list[str]:
        self.refresh()
        return list(self._by_capability.get(capability, []))


# ---- K02/K07: concurrent + lazy loading -------------------------------------------
class LoadCoordinator:
    def __init__(self, lifecycle: LifecycleManager, *, max_workers: int = 4) -> None:
        self.lifecycle = lifecycle
        self.max_workers = max_workers
        self.touched: dict[str, float] = {}
        self._lock = threading.RLock()

    def load_many(self, provider_ids: list[str]) -> dict[str, Any]:
        """Providers are loaded concurrently within a dependency level; levels run in order."""
        levels: dict[str, int] = {}

        def level(pid: str) -> int:
            if pid not in levels:
                levels[pid] = 1 + max([level(d) for d in self.lifecycle.dependencies(pid)] or [-1])
            return levels[pid]
        for pid in provider_ids:
            level(pid)
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for lvl in sorted(set(levels.values())):
                batch = [p for p in levels if levels[p] == lvl and (p in provider_ids or True)]
                for pid, outcome in zip(batch, pool.map(self.lifecycle.load, batch)):
                    results[pid] = outcome.get("status")
        return results

    def ensure_loaded(self, provider_id: str) -> dict[str, Any]:
        """K07: load on first use only (no-op when already loaded)."""
        with self._lock:
            self.touched[provider_id] = time.monotonic()
        if self.lifecycle.cm.get_loaded_provider(provider_id) is not None:
            return {"status": "ALREADY_LOADED", "loaded": True}
        return self.lifecycle.load(provider_id)


# ---- K08: on-demand discovery -----------------------------------------------------
class OnDemandDiscovery:
    def __init__(self, core: RegistryCore) -> None:
        self.core = core
        self._cache: dict[str, tuple[tuple[tuple[str, int], ...], list[Path]]] = {}
        self.scans = 0

    def discover(self, root: str | Path) -> list[Path]:
        root = Path(root)
        files = tuple(sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob("*.py"))) if root.is_dir() else ()
        key = str(root)
        cached = self._cache.get(key)
        if cached and cached[0] == files:
            return cached[1]
        self.scans += 1
        found = self.core.discover_modules(root)
        self._cache[key] = (files, found)
        return found


# ---- K09-K12: multi-worker sync + leader lease --------------------------------------
class WorkerSync:
    """Workers sharing one state directory stay consistent via file locks; refresh() picks up foreign writes."""

    def __init__(self, core: RegistryCore, store: RegistryStore) -> None:
        self.core, self.store = core, store
        self._seen = (-1, -1)

    def refresh(self) -> bool:
        try:
            stamp = (self.store.peek_generation(), self.core.cm.catalog_path.stat().st_mtime_ns)
        except OSError:
            return False
        if stamp == self._seen:
            return False
        self._seen = stamp
        with self.core.cm._catalog_transaction():  # reload under the shared interprocess lock
            pass
        return True


class LeaderLease:
    """Single-writer election for background duties (health sweeps, snapshots): one active, others standby."""

    def __init__(self, lock_dir: str | os.PathLike[str], worker_id: str, *, ttl_seconds: float = 10.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(lock_dir) / "leader.lease"
        self.guard = Path(lock_dir) / "leader.lease.guard"
        self.worker_id, self.ttl, self.clock = worker_id, ttl_seconds, clock

    def _read(self) -> tuple[str, float] | None:
        try:
            owner, stamp = self.path.read_text(encoding="utf-8").split("|")
            return owner, float(stamp)
        except (OSError, ValueError):
            return None

    def acquire(self) -> bool:
        with interprocess_file_lock(self.guard):
            current = self._read()
            now = self.clock()
            if current is None or current[0] == self.worker_id or now - current[1] > self.ttl:
                self.path.write_text(f"{self.worker_id}|{now}", encoding="utf-8")
                return True
            return False

    def is_leader(self) -> bool:
        current = self._read()
        return current is not None and current[0] == self.worker_id and self.clock() - current[1] <= self.ttl

    def release(self) -> None:
        with interprocess_file_lock(self.guard):
            current = self._read()
            if current and current[0] == self.worker_id:
                self.path.unlink(missing_ok=True)


# ---- K15: resource optimisation -----------------------------------------------------
class ResourceOptimizer:
    def __init__(self, lifecycle: LifecycleManager, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.lifecycle, self.clock = lifecycle, clock
        self.last_used: dict[str, float] = {}

    def touch(self, provider_id: str) -> None:
        self.last_used[provider_id] = self.clock()

    def reap_idle(self, idle_seconds: float, *, keep: set[str] | None = None) -> dict[str, Any]:
        """Unload runtime instances idle longer than the threshold (frees sockets/memory); never reaps in-use or kept ones."""
        now, reaped = self.clock(), []
        loaded_at_start = {p["id"] for p in self.lifecycle.cm.list_providers() if self.lifecycle.cm.get_loaded_provider(p["id"]) is not None}
        for pid in sorted(loaded_at_start):                    # decide on a snapshot: dependency chains unwind one pass at a time
            if keep and pid in keep:
                continue
            last = self.last_used.get(pid)
            has_loaded_dependents = any(d in loaded_at_start for d in self.lifecycle.dependents(pid))
            if last is not None and now - last > idle_seconds and not has_loaded_dependents:
                if self.lifecycle.unload(pid).get("status") == "UNLOADED":
                    reaped.append(pid)
        return {"reaped": reaped}
