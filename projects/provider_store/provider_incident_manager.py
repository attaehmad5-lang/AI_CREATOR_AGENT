"""Persistent provider incident deduplication and acknowledgement state."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_CODE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_SEVERITIES = {"info", "warning", "error", "critical"}
_STATES = {"OPEN", "ACKNOWLEDGED", "RESOLVED"}


class ProviderIncidentManager:
    """Persist deduplicated incident metadata only; never store raw exception messages."""

    def __init__(self, state_path: str | os.PathLike[str], *, clock: Any = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "incidents": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "incidents": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid incident state JSON") from exc
        incidents = loaded.get("incidents") if isinstance(loaded, dict) else None
        if not isinstance(incidents, dict):
            raise ValueError("invalid incident state schema")
        for key, incident in incidents.items():
            if (
                not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                or not isinstance(incident, dict)
                or incident.get("state") not in _STATES
                or incident.get("severity") not in _SEVERITIES
                or not isinstance(incident.get("provider_id"), str)
                or not isinstance(incident.get("code"), str)
                or not isinstance(incident.get("occurrences"), int)
            ):
                raise ValueError("invalid incident record")
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def record(self, *, provider_id: str, category: str, severity: str, code: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "saved": False}
        if not isinstance(category, str) or not _ID_RE.fullmatch(category):
            return {"status": "INVALID_INCIDENT_CATEGORY", "saved": False}
        if severity not in _SEVERITIES:
            return {"status": "INVALID_INCIDENT_SEVERITY", "saved": False}
        if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
            return {"status": "INVALID_INCIDENT_CODE", "saved": False}
        key = hashlib.sha256(f"{provider_id}\0{category}\0{code}".encode()).hexdigest()
        now = self.clock().astimezone(timezone.utc).isoformat()
        with self._transaction():
            incident = self.data["incidents"].get(key)
            if isinstance(incident, dict) and incident.get("state") in {"OPEN", "ACKNOWLEDGED"}:
                incident["occurrences"] = min(int(incident.get("occurrences", 1)) + 1, 2**31 - 1)
                incident["last_seen_at"] = now
                if severity == "critical" or (severity == "error" and incident.get("severity") == "warning"):
                    incident["severity"] = severity
                self._save()
                return {"status": "INCIDENT_UPDATED", "saved": True, "incident": dict(incident)}
            incident = {
                "incident_id": key, "provider_id": provider_id, "category": category,
                "severity": severity, "code": code, "state": "OPEN",
                "occurrences": 1, "created_at": now, "last_seen_at": now,
            }
            self.data["incidents"][key] = incident
            self._save()
            return {"status": "INCIDENT_OPENED", "saved": True, "incident": dict(incident)}

    def acknowledge(self, incident_id: str) -> dict[str, Any]:
        return self._transition(incident_id, "ACKNOWLEDGED")

    def resolve(self, incident_id: str) -> dict[str, Any]:
        return self._transition(incident_id, "RESOLVED")

    def _transition(self, incident_id: str, state: str) -> dict[str, Any]:
        if not isinstance(incident_id, str) or not re.fullmatch(r"[0-9a-f]{64}", incident_id):
            return {"status": "INVALID_INCIDENT_ID", "saved": False}
        with self._transaction():
            incident = self.data["incidents"].get(incident_id)
            if not isinstance(incident, dict):
                return {"status": "INCIDENT_NOT_FOUND", "saved": False}
            if incident.get("state") == "RESOLVED":
                return {"status": "INCIDENT_ALREADY_RESOLVED", "saved": True, "incident": dict(incident)}
            incident["state"] = state
            incident["updated_at"] = self.clock().astimezone(timezone.utc).isoformat()
            self._save()
            return {"status": f"INCIDENT_{state}", "saved": True, "incident": dict(incident)}

    def snapshot(self) -> dict[str, Any]:
        with self._transaction():
            incidents = [dict(x) for x in self.data["incidents"].values() if isinstance(x, dict)]
        counts = {state: sum(item.get("state") == state for item in incidents) for state in sorted(_STATES)}
        return {
            "status": "INCIDENTS_AVAILABLE" if incidents else "NO_INCIDENTS",
            "counts": counts,
            "incidents": sorted(incidents, key=lambda x: (x.get("severity", ""), x.get("created_at", ""))),
        }
