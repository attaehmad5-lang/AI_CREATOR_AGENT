"""Intelligent routing & selection (section J).

Registry-level selection (capability, health, performance, quality, cost, priority,
load) that *feeds* the existing ProviderRoutingManager (which keeps owning quota,
credit, project-policy and cost-policy decisions) instead of replacing it.
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .config import ConfigManager
from .core import RegistryCore
from .health import RegistryHealth
from .interface import GenerationRequest, UnifiedInterface
from .storage import RegistryStore

QUALITY_ORDER = {"draft": 25, "standard": 50, "high": 75, "ultra": 100}
STRATEGIES = {"PRIORITY", "PERFORMANCE", "QUALITY", "COST", "RELIABILITY", "BALANCED", "ROUND_ROBIN", "LEAST_LOADED",
              "PRIMARY_THEN_SECONDARY"}
WEIGHTS = {"priority": 0.2, "performance": 0.2, "quality": 0.2, "cost": 0.2, "reliability": 0.2}
AUDIT_LIMIT = 1000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RegistryRouter:
    def __init__(self, core: RegistryCore, store: RegistryStore, interface: UnifiedInterface, health: RegistryHealth,
                 config: ConfigManager | None = None, *, routing_manager: Any | None = None) -> None:
        self.core, self.store, self.cm, self.bus = core, store, core.cm, core.bus
        self.interface, self.health, self.config, self.routing_manager = interface, health, config, routing_manager
        self.policy_hook: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None  # governance plugs in here (P14)
        self._rr = itertools.count()
        self._lock = threading.RLock()
        self._in_flight: dict[str, int] = {}
        self._ewma: dict[str, dict[str, float]] = {}
        self._last_top: dict[str, str] = {}
        with store.transaction() as data:
            data["ext"].setdefault("routing", {"primary": {}, "secondary": {}})
            data["ext"].setdefault("routing_audit", [])

    # ---- primary / secondary (J08, J09) ----
    def set_primary(self, key: str, provider_id: str) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        with self.store.transaction() as data:
            data["ext"]["routing"]["primary"][key] = provider_id
        return {"status": "PRIMARY_SET", "key": key, "provider": provider_id}

    def set_secondary(self, key: str, provider_ids: list[str]) -> dict[str, Any]:
        unknown = [p for p in provider_ids if self.cm.get_provider(p) is None]
        if unknown:
            return {"status": "UNKNOWN_PROVIDERS", "providers": unknown}
        primary = self.store.read()["ext"]["routing"]["primary"].get(key)
        if primary in provider_ids:
            return {"status": "PRIMARY_CANNOT_BE_SECONDARY", "provider": primary}
        with self.store.transaction() as data:
            data["ext"]["routing"]["secondary"][key] = list(dict.fromkeys(provider_ids))
        return {"status": "SECONDARY_SET", "key": key, "providers": list(dict.fromkeys(provider_ids))}

    def chain(self, key: str) -> list[str]:
        routing = self.store.read()["ext"]["routing"]
        head = [routing["primary"][key]] if key in routing["primary"] else []
        return head + list(routing["secondary"].get(key, []))

    # ---- feedback / re-ranking (J14) ----
    def feedback(self, provider_id: str, *, success: bool, latency_ms: float | None = None) -> None:
        with self._lock:
            state = self._ewma.setdefault(provider_id, {"success": 1.0 if success else 0.0, "latency": latency_ms or 0.0})
            state["success"] = 0.7 * state["success"] + 0.3 * (1.0 if success else 0.0)
            if latency_ms is not None:
                state["latency"] = latency_ms if state["latency"] == 0 else 0.7 * state["latency"] + 0.3 * latency_ms

    # ---- load balancing (J12) ----
    def acquire(self, provider_id: str) -> int:
        with self._lock:
            self._in_flight[provider_id] = self._in_flight.get(provider_id, 0) + 1
            return self._in_flight[provider_id]

    def release(self, provider_id: str) -> int:
        with self._lock:
            self._in_flight[provider_id] = max(0, self._in_flight.get(provider_id, 0) - 1)
            return self._in_flight[provider_id]

    def load(self, provider_id: str) -> int:
        with self._lock:
            return self._in_flight.get(provider_id, 0)

    # ---- scoring components ----
    def _components(self, pid: str, estimated_costs: dict[str, float] | None) -> dict[str, float]:
        cfg = self.config.effective(pid)["config"] if self.config else {}
        priority = max(0, min(1000, int(cfg.get("priority", 100))))
        stats = self.health.latency_stats(pid)
        ewma = self._ewma.get(pid)
        latency = ewma["latency"] if ewma and ewma["latency"] else (stats.get("avg_ms") if stats["samples"] else None)
        performance = 50.0 if latency is None else max(0.0, 100 - min(latency, 10_000) / 100)
        meta = self.core.get_metadata(pid) or {}
        quality = float(meta["quality_score"]) if "quality_score" in meta else float(QUALITY_ORDER.get(cfg.get("quality", "standard"), 50))
        cost = (estimated_costs or {}).get(pid, meta.get("cost_per_unit"))
        cost_score = 50.0 if cost is None else max(0.0, 100 - min(float(cost), 10.0) * 10)
        reliability = self.health.reliability_score(pid)["score"]
        if ewma is not None and reliability is not None:
            reliability = 0.5 * reliability + 0.5 * ewma["success"] * 100
        elif ewma is not None:
            reliability = ewma["success"] * 100
        return {"priority": 100 - priority / 10, "performance": performance, "quality": quality, "cost": cost_score,
                "reliability": 50.0 if reliability is None else float(reliability)}

    # ---- candidate filtering (J13) ----
    def candidates(self, *, capability: str | None = None, category: str | None = None, verified_only: bool = False,
                   require_loaded: bool = False, require_health_evidence: bool = False, exclude: set[str] | None = None,
                   context: dict[str, Any] | None = None, **requirements: Any) -> tuple[list[str], list[dict[str, str]]]:
        if capability:
            pool = self.interface.match(capability, verified_only=verified_only)
        elif category:
            pool = [p["id"] for p in self.core.catalog_for(category) if p.get("enabled") is True] or \
                   [p["id"] for p in self.cm.list_providers() if category.casefold() in self.core.categories_of(p["id"]) and p.get("enabled") is True]
        else:
            pool = [p["id"] for p in self.cm.list_providers() if p.get("enabled") is True]
        if category and capability:
            pool = [p for p in pool if category.casefold() in self.core.categories_of(p)]
        kept, decisions = [], []
        for pid in sorted(set(pool)):
            provider = self.cm.get_provider(pid) or {}
            reason = None
            if exclude and pid in exclude:
                reason = "EXCLUDED"
            elif self.core.is_retired(pid):
                reason = "RETIRED"
            elif self.core.is_deprecated(pid) and not (context or {}).get("allow_deprecated"):
                reason = "DEPRECATED"
            elif self.health.is_isolated(pid):
                reason = "ISOLATED"
            elif require_loaded and self.cm.get_loaded_provider(pid) is None:
                reason = "NOT_LOADED"
            elif require_health_evidence and not self.health.eligibility(pid).get("eligible"):
                reason = "HEALTH_INELIGIBLE"
            elif requirements and not self.interface.provider_supports(pid, **requirements):
                reason = "REQUIREMENTS_NOT_MET"
            elif self.policy_hook is not None:
                verdict = self.policy_hook(pid, dict(context or {}))
                if not verdict.get("allowed", False):
                    reason = f"POLICY:{verdict.get('reason', 'DENIED')}"
            decisions.append({"provider": pid, "status": reason or "ELIGIBLE"})
            if reason is None:
                kept.append(pid)
        return kept, decisions

    # ---- selection (J01-J07) ----
    def select(self, *, strategy: str = "BALANCED", limit: int = 1, key: str | None = None,
               estimated_costs: dict[str, float] | None = None, weights: dict[str, float] | None = None,
               caller: str = "api", **filters: Any) -> dict[str, Any]:
        strategy = strategy.upper()
        if strategy not in STRATEGIES:
            return {"allowed": False, "status": "UNSUPPORTED_STRATEGY", "supported": sorted(STRATEGIES)}
        kept, decisions = self.candidates(**filters)
        if not kept:
            return self._finish({"allowed": False, "status": "NO_ELIGIBLE_PROVIDER", "selected": None, "ranked": [],
                                 "decisions": decisions, "strategy": strategy}, caller)
        scored = {pid: self._components(pid, estimated_costs) for pid in kept}
        w = {**WEIGHTS, **(weights or {})}
        if strategy == "PRIORITY":
            order = sorted(kept, key=lambda p: (-scored[p]["priority"], p))
        elif strategy == "PERFORMANCE":
            order = sorted(kept, key=lambda p: (-scored[p]["performance"], p))
        elif strategy == "QUALITY":
            order = sorted(kept, key=lambda p: (-scored[p]["quality"], p))
        elif strategy == "COST":
            order = sorted(kept, key=lambda p: (-scored[p]["cost"], p))
        elif strategy == "RELIABILITY":
            order = sorted(kept, key=lambda p: (-scored[p]["reliability"], p))
        elif strategy == "ROUND_ROBIN":
            start = next(self._rr) % len(kept)
            order = kept[start:] + kept[:start]
        elif strategy == "LEAST_LOADED":
            order = sorted(kept, key=lambda p: (self.load(p), -scored[p]["reliability"], p))
        elif strategy == "PRIMARY_THEN_SECONDARY":
            configured = [p for p in self.chain(key or filters.get("capability") or filters.get("category") or "") if p in kept]
            order = configured + sorted((p for p in kept if p not in configured), key=lambda p: (-scored[p]["reliability"], p))
        else:  # BALANCED
            total = sum(w.values()) or 1.0
            order = sorted(kept, key=lambda p: (-sum(w.get(k, 0) * v for k, v in scored[p].items()) / total, p))
        ranked = [{"provider": p, "components": {k: round(v, 2) for k, v in scored[p].items()}} for p in order]
        top = order[0]
        scope = key or filters.get("capability") or filters.get("category") or "*"
        if self._last_top.get(scope) not in (None, top):
            self.bus.emit("routing.reranked", {"scope": scope, "from": self._last_top[scope], "to": top})  # J14
        self._last_top[scope] = top
        return self._finish({"allowed": True, "status": "SELECTED", "selected": top if limit == 1 else order[:limit],
                             "ranked": ranked, "decisions": decisions, "strategy": strategy}, caller)

    def select_many(self, count: int, **kwargs: Any) -> dict[str, Any]:
        """J11: the top ``count`` distinct providers for parallel / ensemble use."""
        return self.select(limit=max(1, int(count)), **kwargs)

    # ---- audit trail (J15) ----
    def _finish(self, outcome: dict[str, Any], caller: str) -> dict[str, Any]:
        with self.store.transaction() as data:
            seq = 1 + max([e["seq"] for e in data["ext"]["routing_audit"]] or [0])
            data["ext"]["routing_audit"].append({
                "seq": seq, "ts": _now(), "caller": caller, "strategy": outcome["strategy"], "status": outcome["status"],
                "selected": outcome.get("selected"), "ranked": [r["provider"] for r in outcome["ranked"]][:10],
                "excluded": {d["provider"]: d["status"] for d in outcome["decisions"] if d["status"] != "ELIGIBLE"}})
            del data["ext"]["routing_audit"][:-AUDIT_LIMIT]
        outcome["decision_id"] = seq
        self.core.metrics.incr("routing.decisions")
        return outcome

    def audit_trail(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.read()["ext"]["routing_audit"][-max(1, limit):]

    # ---- execution with fallback (J10) ----
    def execute(self, request: GenerationRequest, *, max_attempts: int = 3, **select_kwargs: Any) -> dict[str, Any]:
        """Run the request on the best provider; on failure fall through the ranked chain."""
        select_kwargs.setdefault("require_loaded", True)
        select_kwargs.setdefault("category", request.modality or select_kwargs.get("category"))
        plan = self.select(limit=max(1, max_attempts), caller="execute", **select_kwargs)
        if not plan["allowed"]:
            return {"ok": False, "status": plan["status"], "attempts": [], "decision_id": plan["decision_id"]}
        order = plan["selected"] if isinstance(plan["selected"], list) else [plan["selected"]]
        attempts = []
        for pid in order[:max_attempts]:
            self.acquire(pid)
            try:
                outcome = self.interface.invoke(pid, request)
            finally:
                self.release(pid)
            ok = bool(outcome.get("ok"))
            latency = outcome["result"]["latency_ms"] if ok else None
            self.feedback(pid, success=ok, latency_ms=latency)
            attempts.append({"provider": pid, "ok": ok, "error": None if ok else outcome["error"]["code"]})
            if ok:
                return {"ok": True, "provider": pid, "result": outcome["result"], "attempts": attempts,
                        "fallback_used": len(attempts) > 1, "decision_id": plan["decision_id"]}
            error = outcome["error"]
            if not error["retryable"] and error["code"] in {"INVALID_REQUEST", "UNSUPPORTED_OPERATION"}:
                break  # the request itself is bad; another provider will not help
        return {"ok": False, "status": "ALL_PROVIDERS_FAILED", "attempts": attempts, "decision_id": plan["decision_id"]}

    # ---- hand-off to the existing policy router ----
    def route_with_policies(self, *, category: str, project_id: str, **route_options: Any) -> dict[str, Any]:
        """Registry ineligibility becomes ``excluded_providers`` for ProviderRoutingManager (cost/quota/credit policy)."""
        if self.routing_manager is None:
            return {"allowed": False, "status": "NO_ROUTING_MANAGER"}
        kept, decisions = self.candidates(category=category)
        eligible_all = {p["id"] for p in self.cm.discover_providers(category=category, enabled_only=True)}
        excluded = set(route_options.pop("excluded_providers", None) or set()) | (eligible_all - set(kept))
        outcome = self.routing_manager.route(category=category, project_id=project_id, excluded_providers=excluded, **route_options)
        outcome = dict(outcome)
        outcome["registry_excluded"] = sorted(eligible_all - set(kept))
        return outcome
