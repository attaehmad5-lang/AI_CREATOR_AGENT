"""Registry health & reliability layer (section H) on top of ProviderHealthManager.

ProviderHealthManager remains the single store of health evidence (history,
status transitions, recovery confirmation, staleness). This layer adds: failure
classification, reliability scoring, isolation/recovery events, degradation
detection, scheduled checks and a registry-wide dashboard.
"""
from __future__ import annotations

import statistics
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from ..health_manager import ProviderHealthManager
from .core import RegistryCore
from .storage import RegistryStore

FAILURE_CLASSES = {
    "TIMEOUT": ("TRANSIENT", "timeout"), "NETWORK_ERROR": ("TRANSIENT", "network"), "PROVIDER_UNAVAILABLE": ("TRANSIENT", "server"),
    "RATE_LIMITED": ("TRANSIENT", "rate_limit"), "AUTHENTICATION_FAILED": ("PERMANENT", "authentication"),
    "PERMISSION_DENIED": ("PERMANENT", "permission"), "CREDITS_EXHAUSTED": ("PERMANENT", "credits"),
    "INVALID_REQUEST": ("CALLER_ERROR", "request"), "INVALID_OUTPUT": ("PROVIDER_BUG", "output"),
    "UNSUPPORTED_OPERATION": ("CALLER_ERROR", "unsupported"), "INTERNAL_ERROR": ("UNKNOWN", "internal"),
}


class RegistryHealth:
    def __init__(self, core: RegistryCore, store: RegistryStore, health: ProviderHealthManager, *,
                 degraded_latency_factor: float = 3.0, min_baseline_samples: int = 5) -> None:
        self.core, self.store, self.cm, self.bus, self.hm = core, store, core.cm, core.bus, health
        self.degraded_latency_factor = degraded_latency_factor
        self.min_baseline_samples = min_baseline_samples
        self._timer: threading.Timer | None = None
        self._auto_lock = threading.RLock()
        self._auto_stop = threading.Event()
        with store.transaction() as data:
            data["ext"].setdefault("isolated", {})

    # ---- recording (H01, H06, H07) ----
    def record(self, provider_id: str, *, success: bool, latency_ms: float | None = None, error_code: str | None = None,
              request_id: str | None = None) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        before = self.hm.data["providers"].get(provider_id, {}).get("status", "UNKNOWN")
        result = self.hm.record_result(provider_id, success=success, latency_ms=latency_ms, error_code=error_code, request_id=request_id)
        after = self.hm.data["providers"].get(provider_id, {}).get("status", "UNKNOWN")
        self.core.metrics.incr("health.recorded")
        if not success:
            self.bus.emit("provider.failure", {"provider": provider_id, "error_code": error_code, **self.classify_failure(error_code)})
        self._apply_transitions(provider_id, before, after)
        return result

    def _apply_transitions(self, provider_id: str, before: str, after: str) -> None:
        if after == "UNHEALTHY" and not self.is_isolated(provider_id):
            self._isolate(provider_id, "FAILURE_THRESHOLD_REACHED")  # H11
        elif self.is_isolated(provider_id) and after == "HEALTHY":
            if self.hm.is_eligible(provider_id, stale_after_seconds=3600).get("eligible"):
                self._release(provider_id)  # H12
        if before != after:
            self.bus.emit("provider.health_changed", {"provider": provider_id, "from": before, "to": after})

    # ---- classification (H08) ----
    @staticmethod
    def classify_failure(error_code: str | None) -> dict[str, Any]:
        kind, family = FAILURE_CLASSES.get(str(error_code), ("UNKNOWN", "unknown"))
        return {"failure_kind": kind, "failure_family": family, "retryable": kind == "TRANSIENT"}

    # ---- isolation / recovery (H11, H12) ----
    def _isolate(self, provider_id: str, reason: str) -> None:
        with self.store.transaction() as data:
            data["ext"]["isolated"][provider_id] = {"since": datetime.now(timezone.utc).isoformat(), "reason": reason}
        self.bus.emit("provider.isolated", {"provider": provider_id, "reason": reason})

    def _release(self, provider_id: str) -> None:
        with self.store.transaction() as data:
            data["ext"]["isolated"].pop(provider_id, None)
        self.bus.emit("provider.recovered", {"provider": provider_id})

    def is_isolated(self, provider_id: str) -> bool:
        return provider_id in self.store.read()["ext"]["isolated"]

    def isolated(self) -> dict[str, Any]:
        return dict(self.store.read()["ext"]["isolated"])

    # ---- history / metrics (H05, H06, H10) ----
    def history(self, provider_id: str) -> list[dict[str, Any]]:
        return list(self.hm.data["providers"].get(provider_id, {}).get("history", []))

    def latency_stats(self, provider_id: str) -> dict[str, Any]:
        values = sorted(h["latency_ms"] for h in self.history(provider_id) if isinstance(h.get("latency_ms"), (int, float)))
        if not values:
            return {"samples": 0}
        return {"samples": len(values), "min_ms": values[0], "max_ms": values[-1], "avg_ms": statistics.fmean(values),
                "p95_ms": values[min(len(values) - 1, int(0.95 * len(values)))]}

    def availability(self, provider_id: str) -> float | None:
        """H04: fraction of recorded checks that succeeded (None without evidence)."""
        hist = self.history(provider_id)
        return None if not hist else sum(1 for h in hist if h["success"]) / len(hist)

    # ---- degradation (H14) ----
    def degradation(self, provider_id: str) -> dict[str, Any]:
        lat = [h["latency_ms"] for h in self.history(provider_id) if isinstance(h.get("latency_ms"), (int, float)) and h["success"]]
        reasons = []
        if len(lat) > self.min_baseline_samples:
            baseline = statistics.median(lat[:-1])
            if baseline > 0 and lat[-1] > baseline * self.degraded_latency_factor:
                reasons.append("LATENCY_SPIKE")
        avail = self.availability(provider_id)
        if avail is not None and 0 < avail < 0.8:
            reasons.append("LOW_AVAILABILITY")
        if self.hm.data["providers"].get(provider_id, {}).get("status") == "DEGRADED":
            reasons.append("STATUS_DEGRADED")
        return {"degraded": bool(reasons), "reasons": sorted(set(reasons))}

    # ---- reliability scoring (H09) ----
    def reliability_score(self, provider_id: str) -> dict[str, Any]:
        avail = self.availability(provider_id)
        if avail is None:
            return {"score": None, "reason": "NO_HEALTH_EVIDENCE"}
        record = self.hm.data["providers"].get(provider_id, {})
        score = avail * 70
        stats = self.latency_stats(provider_id)
        if stats["samples"]:
            score += 20 * max(0.0, 1 - min(stats["avg_ms"], 5000) / 5000)
        else:
            score += 10
        score += 10 if record.get("consecutive_failures", 0) == 0 else 0
        if record.get("status") == "UNHEALTHY":
            score = min(score, 30)
        return {"score": round(score, 1), "availability": avail, "status": record.get("status", "UNKNOWN")}

    # ---- eligibility (H13) ----
    def eligibility(self, provider_id: str, *, stale_after_seconds: float = 300.0, max_latency_ms: float | None = None) -> dict[str, Any]:
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return {"eligible": False, "reason": "NOT_FOUND"}
        if provider.get("enabled") is not True:
            return {"eligible": False, "reason": "DISABLED"}
        if self.is_isolated(provider_id):
            return {"eligible": False, "reason": "ISOLATED"}
        return self.hm.is_eligible(provider_id, stale_after_seconds=stale_after_seconds, max_latency_ms=max_latency_ms)

    # ---- checks (H02, H03) ----
    def check(self, provider_id: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """Run the provider's own health() (HealthInterface) through ProviderHealthManager.active_probe."""
        instance = self.cm.get_loaded_provider(provider_id)
        if instance is None or not callable(getattr(instance, "health", None)):
            return {"status": "NO_HEALTH_PROBE", "provider": provider_id}

        def probe(timeout_seconds: float) -> bool:
            report = instance.health()
            return isinstance(report, dict) and report.get("healthy") is True
        before = self.hm.data["providers"].get(provider_id, {}).get("status", "UNKNOWN")
        outcome = self.hm.active_probe(provider_id, probe, timeout_seconds=timeout_seconds)
        self._apply_transitions(provider_id, before, self.hm.data["providers"].get(provider_id, {}).get("status", "UNKNOWN"))
        return outcome

    def check_all_loaded(self) -> dict[str, Any]:
        results = {}
        for provider in self.cm.list_providers():
            if self.cm.get_loaded_provider(provider["id"]) is not None:
                results[provider["id"]] = self.check(provider["id"]).get("status")
        return {"checked": len(results), "results": results}

    def verify_live(self, provider_id: str, probe: Callable[..., Any], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        """H03: real connectivity verification via the repository's existing ConnectionManager.verify_connectivity.

        The probe is called as probe(provider_dict, timeout_seconds=...) and must perform a genuine network request;
        only a persisted success marks CONNECTED.
        """
        outcome = self.cm.verify_connectivity(provider_id, probe, timeout_seconds=timeout_seconds)
        ok = outcome.get("status") in {"CONNECTED", "VERIFIED"} or outcome.get("verified") is True
        self.record(provider_id, success=bool(ok), error_code=None if ok else "NETWORK_ERROR")
        return outcome

    def start_auto_checks(self, interval_seconds: float) -> dict[str, Any]:
        if not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
            return {"status": "INVALID_INTERVAL"}
        with self._auto_lock:
            if self._timer is not None:
                return {"status": "ALREADY_RUNNING"}
            self._auto_stop.clear()

            def tick() -> None:
                if self._auto_stop.is_set():
                    return
                try:
                    self.check_all_loaded()
                finally:
                    with self._auto_lock:
                        if not self._auto_stop.is_set():
                            self._timer = threading.Timer(interval_seconds, tick)
                            self._timer.daemon = True
                            self._timer.start()
            self._timer = threading.Timer(interval_seconds, tick)
            self._timer.daemon = True
            self._timer.start()
        return {"status": "STARTED", "interval_seconds": interval_seconds}

    def stop_auto_checks(self) -> dict[str, Any]:
        with self._auto_lock:
            self._auto_stop.set()
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        return {"status": "STOPPED"}

    # ---- dashboard (H15) ----
    def dashboard(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        rows = []
        for provider in self.cm.list_providers():
            pid = provider["id"]
            status = self.hm.data["providers"].get(pid, {}).get("status", "UNKNOWN")
            counts[status] = counts.get(status, 0) + 1
            score = self.reliability_score(pid)["score"]
            if score is not None:
                rows.append({"provider": pid, "score": score, "status": status})
        rows.sort(key=lambda r: (r["score"], r["provider"]))
        all_lat = [h["latency_ms"] for p in self.hm.data["providers"].values() for h in p.get("history", [])
                   if isinstance(h.get("latency_ms"), (int, float))]
        return {"providers_total": len(self.cm.list_providers()), "status_counts": counts, "isolated": sorted(self.isolated()),
                "lowest_scores": rows[:5], "avg_latency_ms": statistics.fmean(all_lat) if all_lat else None,
                "checked_providers": len(rows)}
