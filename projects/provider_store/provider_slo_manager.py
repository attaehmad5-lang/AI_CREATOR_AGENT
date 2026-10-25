"""Persistent provider SLO definitions evaluated against bounded runtime metrics."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

SLO_POLICY_PATH = Path(__file__).with_name("provider_slo_policies.json")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")


class ProviderSLOManager:
    """Store non-secret SLO thresholds and evaluate them against a metrics snapshot."""

    POLICY_FIELDS = {
        "minimum_samples", "min_success_rate", "max_p95_latency_ms",
        "max_timeout_rate", "max_avg_queue_wait_ms",
    }

    def __init__(self, metrics_manager: Any, path: str | os.PathLike[str] | None = None) -> None:
        if not callable(getattr(metrics_manager, "snapshot", None)):
            raise ValueError("metrics_manager must support snapshot")
        self.metrics_manager = metrics_manager
        self.path = Path(path) if path is not None else SLO_POLICY_PATH
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._lock = threading.RLock()
        with interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
        self._validate_document(self.data)

    def _load_current(self) -> dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except FileNotFoundError:
            return {"version": "1.0", "policies": {}}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise ValueError("SLO policy store is unreadable") from exc
        if not isinstance(loaded, dict):
            raise ValueError("invalid SLO policy document")
        return loaded

    @classmethod
    def _validate_policy(cls, policy: Any) -> list[str]:
        if not isinstance(policy, dict):
            return ["policy must be an object"]
        errors = []
        unknown = set(policy) - cls.POLICY_FIELDS
        if unknown:
            errors.append("unsupported SLO fields: " + ", ".join(sorted(str(x) for x in unknown)))
        if not policy or not set(policy).intersection(cls.POLICY_FIELDS):
            errors.append("at least one SLO threshold is required")
        if "minimum_samples" in policy and (
            isinstance(policy["minimum_samples"], bool)
            or not isinstance(policy["minimum_samples"], int)
            or not 1 <= policy["minimum_samples"] <= 100000
        ):
            errors.append("minimum_samples must be an integer from 1 to 100000")
        for field in ("min_success_rate", "max_timeout_rate"):
            if field in policy:
                value = finite_number(policy[field], minimum=0)
                if value is None or value > 1:
                    errors.append(f"{field} must be between 0 and 1")
        for field in ("max_p95_latency_ms", "max_avg_queue_wait_ms"):
            if field in policy and finite_number(policy[field], minimum=0) is None:
                errors.append(f"{field} must be a finite non-negative number")
        return errors

    @classmethod
    def _validate_document(cls, data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != "1.0" or not isinstance(data.get("policies"), dict):
            raise ValueError("invalid SLO policy document schema")
        for key, policy in data["policies"].items():
            if not isinstance(key, str) or "|" not in key:
                raise ValueError("invalid SLO policy key")
            provider_id, category = key.split("|", 1)
            if not _ID_RE.fullmatch(provider_id) or not _ID_RE.fullmatch(category):
                raise ValueError("invalid SLO policy identity")
            errors = cls._validate_policy(policy)
            if errors:
                raise ValueError("invalid SLO policy: " + "; ".join(errors))

    @staticmethod
    def _key(provider_id: str, category: str) -> str:
        if not isinstance(provider_id, str) or not _ID_RE.fullmatch(provider_id):
            raise ValueError("invalid provider_id")
        if not isinstance(category, str) or not _ID_RE.fullmatch(category):
            raise ValueError("invalid category")
        return f"{provider_id}|{category}"

    def _save_unlocked(self) -> None:
        self._validate_document(self.data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def set_policy(self, provider_id: str, category: str, policy: dict[str, Any]) -> dict[str, Any]:
        try:
            key = self._key(provider_id, category)
        except ValueError as exc:
            return {"status": "INVALID_SLO_IDENTITY", "saved": False, "reason": str(exc)}
        errors = self._validate_policy(policy)
        if errors:
            return {"status": "INVALID_SLO_POLICY", "saved": False, "errors": errors}
        safe = dict(policy)
        for field in ("min_success_rate", "max_timeout_rate", "max_p95_latency_ms", "max_avg_queue_wait_ms"):
            if field in safe:
                safe[field] = finite_number(safe[field], minimum=0)
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            self.data["policies"][key] = safe
            self._save_unlocked()
        return {"status": "SLO_POLICY_SAVED", "saved": True, "provider_id": provider_id, "category": category}

    def evaluate(self, *, provider_id: str, category: str) -> dict[str, Any]:
        try:
            key = self._key(provider_id, category)
        except ValueError as exc:
            return {"status": "INVALID_SLO_IDENTITY", "compliant": False, "reason": str(exc)}
        with self._lock, interprocess_file_lock(self.lock_path):
            self.data = self._load_current()
            self._validate_document(self.data)
            policy = self.data["policies"].get(key)
        if not isinstance(policy, dict):
            return {"status": "SLO_POLICY_NOT_FOUND", "compliant": False}
        try:
            snapshot = self.metrics_manager.snapshot(provider_id=provider_id, category=category)
        except Exception as exc:
            return {"status": "SLO_METRICS_UNAVAILABLE", "compliant": False, "error_type": type(exc).__name__}
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metrics"), dict):
            return {"status": "INVALID_SLO_METRICS", "compliant": False}
        metrics = snapshot["metrics"]
        sample_count = metrics.get("sample_count")
        minimum_samples = policy.get("minimum_samples", 1)
        if (
            isinstance(sample_count, bool) or not isinstance(sample_count, int)
            or sample_count < minimum_samples
            or metrics.get("success_rate") is None
        ):
            return {
                "status": "SLO_DATA_INSUFFICIENT", "compliant": None,
                "sample_count": sample_count, "minimum_samples": minimum_samples,
            }
        breaches = []
        success_rate = metrics["success_rate"]
        if "min_success_rate" in policy and success_rate < policy["min_success_rate"]:
            breaches.append("SUCCESS_RATE_BELOW_TARGET")
        if "max_p95_latency_ms" in policy and metrics.get("p95_latency_ms") is not None and metrics["p95_latency_ms"] > policy["max_p95_latency_ms"]:
            breaches.append("P95_LATENCY_ABOVE_TARGET")
        timeout_rate = metrics.get("timeout_count", 0) / sample_count
        if "max_timeout_rate" in policy and timeout_rate > policy["max_timeout_rate"]:
            breaches.append("TIMEOUT_RATE_ABOVE_TARGET")
        if "max_avg_queue_wait_ms" in policy and metrics.get("avg_queue_wait_ms") is not None and metrics["avg_queue_wait_ms"] > policy["max_avg_queue_wait_ms"]:
            breaches.append("QUEUE_WAIT_ABOVE_TARGET")
        return {
            "status": "SLO_BREACH" if breaches else "SLO_COMPLIANT",
            "compliant": not breaches,
            "provider_id": provider_id,
            "category": category,
            "sample_count": sample_count,
            "breaches": breaches,
            "metrics": dict(metrics),
            "policy": dict(policy),
        }
