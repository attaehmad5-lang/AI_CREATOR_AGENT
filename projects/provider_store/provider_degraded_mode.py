"""Persistent category-level degraded-mode decisions with recovery hysteresis."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .encrypted_state_store import EncryptedJSONStateStore
from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")
_MODES = {"NORMAL", "DEGRADED", "OUTAGE"}


class ProviderDegradedModeManager:
    """Track service-category health and persist safe mode transitions."""

    def __init__(
        self,
        state_path: str | os.PathLike[str],
        *,
        clock: Any = None,
        max_categories: int = 1000,
        state_encryption_key_provider: Any = None,
        state_encryption_context: str = "provider-degraded-mode-v1",
        migrate_legacy_plaintext_state: bool = False,
    ) -> None:
        if state_encryption_key_provider is None and migrate_legacy_plaintext_state:
            raise ValueError("legacy state migration requires an encryption key provider")
        if isinstance(max_categories, bool) or not isinstance(max_categories, int) or not 1 <= max_categories <= 100_000:
            raise ValueError("max_categories must be within safe limits")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.state_path = Path(state_path)
        self.state_store = (
            EncryptedJSONStateStore(
                self.state_path,
                key_provider=state_encryption_key_provider,
                context=state_encryption_context,
                migrate_legacy_plaintext=migrate_legacy_plaintext_state,
            )
            if state_encryption_key_provider is not None else None
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_categories = max_categories
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "version": "1.0", "policies": {}, "states": {},
        }
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        with interprocess_file_lock(lock_path):
            self._reload()
            if self.state_store is not None and self.state_store.migration_required:
                self._save()

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, str) and _ID_RE.fullmatch(value) is not None

    @staticmethod
    def _now(clock: Any) -> datetime:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("timezone-aware clock value required")
        return value.astimezone(timezone.utc)

    def _reload(self) -> None:
        default = {"version": "1.0", "policies": {}, "states": {}}
        if self.state_store is not None:
            loaded = self.state_store.read(default)
            if not self.state_path.exists():
                self.data = loaded
                return
        else:
            if not self.state_path.exists():
                self.data = default
                return
            try:
                with self.state_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
                raise ValueError("invalid degraded-mode state JSON") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("version") != "1.0"
            or not isinstance(loaded.get("policies"), dict)
            or not isinstance(loaded.get("states"), dict)
            or len(loaded["policies"]) > self.max_categories
            or len(loaded["states"]) > self.max_categories
        ):
            raise ValueError("invalid degraded-mode schema or capacity")
        for category, policy in loaded["policies"].items():
            if (
                not self._valid_id(category) or not isinstance(policy, dict)
                or isinstance(policy.get("normal_min_healthy_providers"), bool)
                or not isinstance(policy.get("normal_min_healthy_providers"), int)
                or not 1 <= policy["normal_min_healthy_providers"] <= 1000
                or isinstance(policy.get("degraded_min_healthy_providers"), bool)
                or not isinstance(policy.get("degraded_min_healthy_providers"), int)
                or not 1 <= policy["degraded_min_healthy_providers"] <= 1000
                or policy["degraded_min_healthy_providers"] > policy["normal_min_healthy_providers"]
                or isinstance(policy.get("recovery_success_samples"), bool)
                or not isinstance(policy.get("recovery_success_samples"), int)
                or not 1 <= policy["recovery_success_samples"] <= 100
            ):
                raise ValueError("invalid degraded-mode policy")
        for category, state in loaded["states"].items():
            if (
                not self._valid_id(category) or not isinstance(state, dict)
                or state.get("mode") not in _MODES
                or isinstance(state.get("consecutive_healthy_cycles"), bool)
                or not isinstance(state.get("consecutive_healthy_cycles"), int)
                or state["consecutive_healthy_cycles"] < 0
                or isinstance(state.get("transition_count"), bool)
                or not isinstance(state.get("transition_count"), int)
                or state["transition_count"] < 0
                or not isinstance(state.get("healthy_provider_ids"), list)
                or not isinstance(state.get("unhealthy_provider_ids"), list)
                or any(not self._valid_id(item) for item in state["healthy_provider_ids"] + state["unhealthy_provider_ids"])
                or not isinstance(state.get("last_evaluated_at"), str)
            ):
                raise ValueError("invalid degraded-mode state record")
            try:
                parsed = datetime.fromisoformat(state["last_evaluated_at"])
                if parsed.tzinfo is None:
                    raise ValueError("timezone required")
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("invalid degraded-mode timestamp") from exc
        self.data = loaded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            lock_path = self.state_path.with_name(self.state_path.name + ".lock")
            with interprocess_file_lock(lock_path):
                self._reload()
                yield

    def _save(self) -> None:
        if self.state_store is not None:
            self.state_store.write(self.data)
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.", suffix=".tmp",
            dir=str(self.state_path.parent), text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
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

    def configure(
        self,
        category: str,
        *,
        normal_min_healthy_providers: int = 2,
        degraded_min_healthy_providers: int = 1,
        recovery_success_samples: int = 3,
    ) -> dict[str, Any]:
        if not self._valid_id(category):
            return {"status": "INVALID_CATEGORY", "saved": False}
        for value, name, minimum, maximum in (
            (normal_min_healthy_providers, "normal_min_healthy_providers", 1, 1000),
            (degraded_min_healthy_providers, "degraded_min_healthy_providers", 1, 1000),
            (recovery_success_samples, "recovery_success_samples", 1, 100),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if degraded_min_healthy_providers > normal_min_healthy_providers:
            return {"status": "INVALID_HEALTHY_PROVIDER_THRESHOLDS", "saved": False}
        policy = {
            "normal_min_healthy_providers": normal_min_healthy_providers,
            "degraded_min_healthy_providers": degraded_min_healthy_providers,
            "recovery_success_samples": recovery_success_samples,
        }
        with self._transaction():
            if category not in self.data["policies"] and len(self.data["policies"]) >= self.max_categories:
                return {"status": "CATEGORY_CAPACITY_REACHED", "saved": False}
            self.data["policies"][category] = policy
            self._save()
        return {"status": "DEGRADED_MODE_POLICY_SAVED", "saved": True, "category": category, "policy": dict(policy)}

    def evaluate(
        self,
        category: str,
        provider_statuses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self._valid_id(category):
            return {"status": "INVALID_CATEGORY", "evaluated": False}
        if not isinstance(provider_statuses, list) or not 1 <= len(provider_statuses) <= 1000:
            return {"status": "INVALID_PROVIDER_STATUS_LIST", "evaluated": False}
        healthy: list[str] = []
        unhealthy: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(provider_statuses):
            if not isinstance(item, dict):
                return {"status": "INVALID_PROVIDER_STATUS", "evaluated": False, "index": index}
            provider_id = item.get("provider_id")
            eligible = item.get("eligible")
            if not self._valid_id(provider_id) or provider_id in seen or not isinstance(eligible, bool):
                return {"status": "INVALID_PROVIDER_STATUS", "evaluated": False, "index": index}
            seen.add(provider_id)
            (healthy if eligible else unhealthy).append(provider_id)
        try:
            now = self._now(self.clock)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return {"status": "INVALID_CLOCK", "evaluated": False}

        with self._transaction():
            policy = self.data["policies"].get(category)
            if not isinstance(policy, dict):
                return {"status": "DEGRADED_MODE_POLICY_NOT_CONFIGURED", "evaluated": False}
            previous = self.data["states"].get(category)
            previous_mode = previous.get("mode", "NORMAL") if isinstance(previous, dict) else "NORMAL"
            previous_streak = previous.get("consecutive_healthy_cycles", 0) if isinstance(previous, dict) else 0
            healthy_count = len(healthy)
            normal_threshold = policy["normal_min_healthy_providers"]
            degraded_threshold = policy["degraded_min_healthy_providers"]

            if healthy_count < degraded_threshold:
                mode = "OUTAGE"
                streak = 0
            elif healthy_count < normal_threshold:
                mode = "DEGRADED"
                streak = 0
            elif previous_mode in {"DEGRADED", "OUTAGE"}:
                streak = previous_streak + 1
                mode = (
                    "NORMAL"
                    if streak >= policy["recovery_success_samples"]
                    else previous_mode
                )
            else:
                mode = "NORMAL"
                streak = policy["recovery_success_samples"]

            transition_count = previous.get("transition_count", 0) if isinstance(previous, dict) else 0
            if mode != previous_mode:
                transition_count += 1
            state = {
                "category": category,
                "mode": mode,
                "healthy_provider_ids": sorted(healthy),
                "unhealthy_provider_ids": sorted(unhealthy),
                "healthy_count": healthy_count,
                "provider_count": len(provider_statuses),
                "consecutive_healthy_cycles": streak,
                "transition_count": transition_count,
                "last_evaluated_at": now.isoformat(),
            }
            self.data["states"][category] = state
            self._save()

        if mode == "NORMAL":
            actions = []
        elif mode == "DEGRADED":
            actions = ["LIMIT_TO_HEALTHY_PROVIDERS", "REQUIRE_FRESH_HEALTH_EVIDENCE"]
        else:
            actions = ["BLOCK_NEW_PROVIDER_EXECUTION", "PRESERVE_STARTED_REQUEST_ACCOUNTING"]
        return {
            "status": f"PROVIDER_CATEGORY_{mode}",
            "evaluated": True,
            "state": dict(state),
            "actions": actions,
        }

    def get_state(self, category: str) -> dict[str, Any]:
        if not self._valid_id(category):
            return {"status": "INVALID_CATEGORY", "state": None}
        with self._transaction():
            state = self.data["states"].get(category)
            if not isinstance(state, dict):
                return {"status": "DEGRADED_MODE_STATE_UNKNOWN", "state": None}
            return {"status": f"PROVIDER_CATEGORY_{state['mode']}", "state": dict(state)}
