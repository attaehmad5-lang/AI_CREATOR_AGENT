"""Deterministic provider A/B cohort assignment with explicit paid-usage guards."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock
from .safe_numbers import finite_number

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}\Z")


class ProviderABExperimentManager:
    def __init__(self, state_path: str | os.PathLike[str]) -> None:
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {"version": "1.0", "experiments": {}}
        self._reload()

    def _reload(self) -> None:
        if not self.state_path.exists():
            self.data = {"version": "1.0", "experiments": {}}
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid experiment state JSON") from exc
        experiments = loaded.get("experiments") if isinstance(loaded, dict) else None
        if not isinstance(experiments, dict):
            raise ValueError("invalid experiment state schema")
        for name, item in experiments.items():
            if (
                not isinstance(name, str) or not _ID_RE.fullmatch(name)
                or not isinstance(item, dict)
                or not isinstance(item.get("control_provider_id"), str)
                or not isinstance(item.get("treatment_provider_id"), str)
                or item.get("control_provider_id") == item.get("treatment_provider_id")
                or not isinstance(item.get("enabled"), bool)
                or isinstance(item.get("rollout_percent"), bool)
                or not isinstance(item.get("rollout_percent"), (int, float))
                or not 0 <= item["rollout_percent"] <= 100
            ):
                raise ValueError("invalid experiment record")
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

    def configure(
        self, experiment_id: str, *, control_provider_id: str,
        treatment_provider_id: str, rollout_percent: float, enabled: bool = True,
    ) -> dict[str, Any]:
        for value, name in (
            (experiment_id, "experiment_id"),
            (control_provider_id, "control_provider_id"),
            (treatment_provider_id, "treatment_provider_id"),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "saved": False}
        if control_provider_id == treatment_provider_id:
            return {"status": "EXPERIMENT_PROVIDERS_MUST_DIFFER", "saved": False}
        if not isinstance(enabled, bool):
            return {"status": "INVALID_ENABLED_FLAG", "saved": False}
        if isinstance(rollout_percent, bool) or not isinstance(rollout_percent, (int, float)) or not 0 <= rollout_percent <= 100:
            return {"status": "INVALID_ROLLOUT_PERCENT", "saved": False}
        with self._transaction():
            self.data["experiments"][experiment_id] = {
                "control_provider_id": control_provider_id,
                "treatment_provider_id": treatment_provider_id,
                "rollout_percent": float(rollout_percent),
                "enabled": enabled,
            }
            self._save()
        return {"status": "EXPERIMENT_CONFIGURED", "saved": True, "experiment_id": experiment_id}

    def select(
        self, experiment_id: str, *, project_id: str, request_id: str,
        candidate_provider_ids: list[str], candidate_costs: dict[str, float],
        zero_cost_provider_ids: list[str], paid_usage_approved: bool,
    ) -> dict[str, Any]:
        for value, name in ((experiment_id, "experiment_id"), (project_id, "project_id"), (request_id, "request_id")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                return {"status": f"INVALID_{name.upper()}", "selected_provider_id": None}
        if not isinstance(candidate_provider_ids, list) or any(
            not isinstance(p, str) or not _ID_RE.fullmatch(p) for p in candidate_provider_ids
        ):
            return {"status": "INVALID_CANDIDATE_PROVIDERS", "selected_provider_id": None}
        if not isinstance(candidate_costs, dict) or not isinstance(zero_cost_provider_ids, list):
            return {"status": "INVALID_COST_POLICY_INPUT", "selected_provider_id": None}
        if not isinstance(paid_usage_approved, bool):
            return {"status": "INVALID_PAID_APPROVAL_FLAG", "selected_provider_id": None}
        costs: dict[str, float] = {}
        for provider_id in candidate_provider_ids:
            value = finite_number(candidate_costs.get(provider_id), minimum=0)
            if value is None:
                return {"status": "EXPERIMENT_COST_UNVERIFIED", "selected_provider_id": None}
            costs[provider_id] = value
        if any(provider_id not in candidate_provider_ids for provider_id in zero_cost_provider_ids):
            return {"status": "INVALID_ZERO_COST_PROVIDER", "selected_provider_id": None}
        with self._transaction():
            experiment = self.data["experiments"].get(experiment_id)
            if not isinstance(experiment, dict):
                return {"status": "EXPERIMENT_UNKNOWN", "selected_provider_id": None}
            control = experiment["control_provider_id"]
            treatment = experiment["treatment_provider_id"]
            if control not in candidate_provider_ids or treatment not in candidate_provider_ids:
                return {"status": "EXPERIMENT_CANDIDATE_UNAVAILABLE", "selected_provider_id": None}
            digest = hashlib.sha256(f"{experiment_id}\0{project_id}\0{request_id}".encode()).digest()
            bucket = int.from_bytes(digest[:8], "big") % 10000
            use_treatment = experiment["enabled"] and bucket < int(float(experiment["rollout_percent"]) * 100)
            selected = treatment if use_treatment else control
            variant = "treatment" if use_treatment else "control"
            status = "EXPERIMENT_TREATMENT_SELECTED" if use_treatment else "EXPERIMENT_CONTROL_SELECTED"
            if costs[selected] > 0 and not paid_usage_approved:
                safe_candidates = [
                    provider_id for provider_id in zero_cost_provider_ids
                    if costs.get(provider_id, float("inf")) == 0
                ]
                if control in safe_candidates:
                    selected = control
                    variant = "control"
                    status = "PAID_EXPERIMENT_BLOCKED_CONTROL_SELECTED"
                elif safe_candidates:
                    selected = safe_candidates[0]
                    variant = "safe_fallback"
                    status = "PAID_EXPERIMENT_BLOCKED_ZERO_COST_FALLBACK"
                else:
                    return {
                        "status": "PAID_EXPERIMENT_BLOCKED",
                        "selected_provider_id": None,
                        "experiment_id": experiment_id,
                        "variant": variant,
                        "bucket": bucket,
                    }
            return {
                "status": status,
                "selected_provider_id": selected,
                "experiment_id": experiment_id,
                "variant": variant,
                "bucket": bucket,
                "paid_usage_approved": paid_usage_approved,
            }
