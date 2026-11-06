"""Durable registry overlay state: atomic writes, integrity checksum, snapshots, repair.

The provider catalog stays owned by ConnectionManager. This store only holds
registry-owned data (tags, groups, config, dependencies, history, policies...).
It reuses the repository's interprocess lock + atomic-replace helpers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

SCHEMA_VERSION = "1.0"


class RegistryCorruptionError(RuntimeError):
    """State file is unreadable or fails its integrity checksum."""


class RegistryReadOnlyError(RuntimeError):
    """Mutation attempted while the registry is in disaster-recovery (read-only) mode."""


def _digest(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "meta": {}, "history": [], "config": {}, "deps": {},
            "tags": {}, "groups": {}, "policies": {}, "tenants": {}, "audit": [], "ext": {}}


class RegistryStore:
    def __init__(self, state_path: str | os.PathLike[str], *, snapshot_dir: str | os.PathLike[str] | None = None,
                 max_snapshots: int = 10, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(state_path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else self.path.with_name(self.path.stem + "_snapshots")
        if not isinstance(max_snapshots, int) or isinstance(max_snapshots, bool) or max_snapshots < 1:
            raise ValueError("max_snapshots must be a positive integer")
        self.max_snapshots = max_snapshots
        self._clock = clock
        self._rlock = threading.RLock()
        self._depth = 0
        self._data: dict[str, Any] = _empty()
        self._dirty = False
        self._cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None
        self.read_only = False  # set by disaster-recovery mode; transactions then raise
        self.generation = 0  # increments on every persisted change (used for cache invalidation/sync)
        self._load_locked_or_init()

    # ---- load / save -------------------------------------------------
    def _read_file(self, path: Path) -> dict[str, Any]:
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistryCorruptionError(f"unreadable registry state: {type(exc).__name__}") from exc
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("body"), dict):
            raise RegistryCorruptionError("registry state has no body")
        if wrapper.get("sha256") != _digest(wrapper["body"]):
            raise RegistryCorruptionError("registry state checksum mismatch")
        body = wrapper["body"]
        for key, default in _empty().items():
            body.setdefault(key, copy.deepcopy(default))
        return body

    def _load_locked_or_init(self) -> None:
        with self._rlock, interprocess_file_lock(self.lock_path):
            if not self.path.exists():
                self._data = _empty()
                self._write_unlocked()
                return
            self._data = self._read_file(self.path)  # raises RegistryCorruptionError on damage
            self.generation = int(self._data.get("_generation", 0))

    def _write_unlocked(self) -> None:
        self.generation += 1
        self._data["_generation"] = self.generation
        self._data["updated_at"] = _now()
        wrapper = {"sha256": _digest(self._data), "body": self._data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(wrapper, stream, indent=1, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp, self.path)
            self._cache = None  # next view() re-validates against the new stamp
        except Exception:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise

    @contextmanager
    def transaction(self, *, allow_read_only: bool = False) -> Iterator[dict[str, Any]]:
        """Exclusive, re-entrant read-modify-write. Reloads from disk so other workers' changes are seen.

        ``allow_read_only`` is reserved for the recovery/audit path: a registry in disaster mode must still be able
        to record what is being done to it."""
        with self._rlock:
            outer = self._depth == 0
            if outer and self.read_only and not allow_read_only:
                raise RegistryReadOnlyError("registry is in read-only (disaster recovery) mode")
            if outer:
                cm = interprocess_file_lock(self.lock_path)
                cm.__enter__()
                try:
                    if self.path.exists():
                        self._data = self._read_file(self.path)
                        self.generation = int(self._data.get("_generation", self.generation))
                    snapshot = copy.deepcopy(self._data)
                except BaseException:
                    cm.__exit__(None, None, None)
                    raise
                self._dirty = False
            self._depth += 1
            try:
                yield self._data
                if outer:
                    if self._data != snapshot or self._dirty:
                        self._write_unlocked()
            except BaseException:
                if outer:
                    self._data = snapshot  # roll back in-memory view on failure
                raise
            finally:
                self._depth -= 1
                if outer:
                    cm.__exit__(None, None, None)

    def _stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def view(self) -> dict[str, Any]:
        """Read-only, stat-validated view (no copy, no lock when unchanged). Callers MUST NOT mutate it.

        The cache is keyed by (mtime_ns, size, inode); every write is an atomic replace, so any write
        by any process changes the stamp and forces a verified re-read (checksum included).
        """
        with self._rlock:
            stamp = self._stamp()
            if stamp is not None and self._cache is not None and self._cache[0] == stamp:
                return self._cache[1]
            with interprocess_file_lock(self.lock_path):
                stamp = self._stamp()
                body = self._read_file(self.path)
            self._cache = (stamp, body) if stamp is not None else None
            return body

    def read(self) -> dict[str, Any]:
        """Detached copy of the verified state (sees other workers' writes)."""
        return copy.deepcopy(self.view())

    def peek_generation(self) -> int:
        try:
            return int(self.view().get("_generation", 0))
        except (RegistryCorruptionError, OSError):
            return -1

    # ---- snapshots / backup ------------------------------------------
    def _snapshot_files(self) -> list[Path]:
        if not self.snapshot_dir.is_dir():
            return []
        return sorted(self.snapshot_dir.glob("snap_*.json"))

    def create_snapshot(self, label: str = "manual") -> dict[str, Any]:
        label = "".join(ch for ch in str(label) if ch.isalnum() or ch in "-_")[:40] or "manual"
        with self._rlock, interprocess_file_lock(self.lock_path):
            body = self._read_file(self.path)
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            seq = 1 + max([int(p.stem.split("_")[1]) for p in self._snapshot_files()] or [0])
            snap_id = f"snap_{seq:06d}_{label}"
            target = self.snapshot_dir / f"{snap_id}.json"
            wrapper = {"id": snap_id, "label": label, "created_at": _now(), "sha256": _digest(body), "body": body}
            fd, temp = tempfile.mkstemp(prefix=".snap.", suffix=".tmp", dir=str(self.snapshot_dir), text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(wrapper, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp, target)
            pruned = self._prune_unlocked()
        return {"status": "SNAPSHOT_CREATED", "id": snap_id, "sha256": wrapper["sha256"], "pruned": pruned}

    def _prune_unlocked(self) -> list[str]:
        files = self._snapshot_files()
        removed = []
        for old in files[: max(0, len(files) - self.max_snapshots)]:
            old.unlink(missing_ok=True)
            removed.append(old.stem)
        return removed

    def list_snapshots(self) -> list[dict[str, Any]]:
        items = []
        for path in self._snapshot_files():
            verdict = self.verify_snapshot(path.stem)
            items.append({"id": path.stem, "valid": verdict["valid"], "size": path.stat().st_size})
        return items

    def verify_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith("snap_") or "/" in snapshot_id or "\\" in snapshot_id or ".." in snapshot_id:
            return {"id": snapshot_id, "valid": False, "reason": "INVALID_SNAPSHOT_ID"}
        path = self.snapshot_dir / f"{snapshot_id}.json"
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"id": snapshot_id, "valid": False, "reason": "NOT_FOUND"}
        except (OSError, ValueError):
            return {"id": snapshot_id, "valid": False, "reason": "UNREADABLE"}
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("body"), dict):
            return {"id": snapshot_id, "valid": False, "reason": "MALFORMED"}
        if wrapper.get("sha256") != _digest(wrapper["body"]):
            return {"id": snapshot_id, "valid": False, "reason": "CHECKSUM_MISMATCH"}
        return {"id": snapshot_id, "valid": True, "sha256": wrapper["sha256"]}

    def validate_restore(self, snapshot_id: str) -> dict[str, Any]:
        verdict = self.verify_snapshot(snapshot_id)
        if not verdict["valid"]:
            return {**verdict, "restorable": False}
        body = json.loads((self.snapshot_dir / f"{snapshot_id}.json").read_text(encoding="utf-8"))["body"]
        if body.get("schema") != SCHEMA_VERSION:
            return {"id": snapshot_id, "valid": True, "restorable": False, "reason": "SCHEMA_MISMATCH"}
        return {"id": snapshot_id, "valid": True, "restorable": True}

    def restore_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        check = self.validate_restore(snapshot_id)
        if not check.get("restorable"):
            return {"status": "RESTORE_REFUSED", **check}
        body = json.loads((self.snapshot_dir / f"{snapshot_id}.json").read_text(encoding="utf-8"))["body"]
        with self._rlock, interprocess_file_lock(self.lock_path):
            self._data = body
            self.generation = int(body.get("_generation", 0))
            self._write_unlocked()
        return {"status": "RESTORED", "id": snapshot_id}

    def detect_corruption(self) -> dict[str, Any]:
        try:
            with self._rlock, interprocess_file_lock(self.lock_path):
                self._read_file(self.path)
            return {"corrupted": False}
        except RegistryCorruptionError as exc:
            return {"corrupted": True, "reason": str(exc)}

    def repair(self) -> dict[str, Any]:
        """Restore the newest valid snapshot if the live state is corrupted."""
        if not self.detect_corruption()["corrupted"]:
            return {"status": "NOT_NEEDED"}
        for path in reversed(self._snapshot_files()):
            if self.validate_restore(path.stem).get("restorable"):
                result = self.restore_snapshot(path.stem)
                return {"status": "REPAIRED", "from": path.stem, **{k: v for k, v in result.items() if k != "status"}}
        return {"status": "REPAIR_FAILED", "reason": "NO_VALID_SNAPSHOT"}
