"""Bounded in-process admission control for provider execution backpressure."""
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}")


class ProviderAdmissionController:
    """Reject overload before routing; retain slots for ambiguous started requests."""

    def __init__(
        self,
        *,
        max_active: int = 32,
        max_active_per_project: int = 8,
        clock: Any = time.monotonic,
    ) -> None:
        for name, value in (
            ("max_active", max_active),
            ("max_active_per_project", max_active_per_project),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 100_000:
                raise ValueError(f"{name} must be a positive bounded integer")
        if max_active_per_project > max_active:
            raise ValueError("per-project limit cannot exceed global active limit")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.max_active = max_active
        self.max_active_per_project = max_active_per_project
        self.clock = clock
        self._lock = threading.RLock()
        self._active: dict[str, dict[str, Any]] = {}
        self._completed: deque[str] = deque(maxlen=4096)
        self._completed_set: set[str] = set()
        self._closed = False

    def _remember_completed(self, request_id: str) -> None:
        if len(self._completed) == self._completed.maxlen:
            oldest = self._completed[0]
            self._completed_set.discard(oldest)
        self._completed.append(request_id)
        self._completed_set.add(request_id)

    def acquire(self, *, request_id: str, project_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return {"acquired": False, "status": "INVALID_REQUEST_ID"}
        if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
            return {"acquired": False, "status": "INVALID_PROJECT_ID"}
        with self._lock:
            if self._closed:
                return {"acquired": False, "status": "PROVIDER_ADMISSION_CLOSED"}
            if request_id in self._active:
                return {"acquired": False, "status": "DUPLICATE_ACTIVE_REQUEST"}
            if request_id in self._completed_set:
                return {"acquired": False, "status": "DUPLICATE_COMPLETED_REQUEST"}
            if len(self._active) >= self.max_active:
                return {"acquired": False, "status": "BACKPRESSURE_GLOBAL_LIMIT"}
            project_active = sum(1 for item in self._active.values() if item["project_id"] == project_id)
            if project_active >= self.max_active_per_project:
                return {"acquired": False, "status": "BACKPRESSURE_PROJECT_LIMIT"}
            now = self.clock()
            if not isinstance(now, (int, float)) or isinstance(now, bool):
                return {"acquired": False, "status": "INVALID_ADMISSION_CLOCK"}
            self._active[request_id] = {
                "project_id": project_id,
                "acquired_at": float(now),
            }
            return {
                "acquired": True,
                "status": "ADMITTED",
                "request_id": request_id,
                "active": len(self._active),
            }

    def release(self, *, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return {"released": False, "status": "INVALID_REQUEST_ID"}
        with self._lock:
            if request_id not in self._active:
                if request_id in self._completed_set:
                    return {"released": False, "status": "ALREADY_RELEASED"}
                return {"released": False, "status": "ADMISSION_LEASE_NOT_FOUND"}
            self._active.pop(request_id)
            self._remember_completed(request_id)
            return {"released": True, "status": "ADMISSION_RELEASED", "active": len(self._active)}

    def shutdown(self) -> dict[str, Any]:
        with self._lock:
            self._closed = True
            return {"status": "ADMISSION_SHUTDOWN", "active": len(self._active)}

    def reopen(self) -> dict[str, Any]:
        with self._lock:
            if self._active:
                return {"status": "ACTIVE_REQUESTS_REMAIN", "reopened": False}
            self._closed = False
            return {"status": "ADMISSION_REOPENED", "reopened": True}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            per_project: dict[str, int] = {}
            for item in self._active.values():
                project_id = item["project_id"]
                per_project[project_id] = per_project.get(project_id, 0) + 1
            return {
                "status": "ADMISSION_SNAPSHOT",
                "active": len(self._active),
                "max_active": self.max_active,
                "max_active_per_project": self.max_active_per_project,
                "per_project": per_project,
                "closed": self._closed,
            }
