"""Event system, structured logging, metrics and alerting for the registry (section L)."""
from __future__ import annotations

import fnmatch
import itertools
import json
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..connection_manager import ConnectionManager


def sanitize(value: Any) -> Any:
    """Strip credential-like values using the repository's existing sanitizer."""
    return ConnectionManager._sanitize_public_data(value)


class MetricsCollector:
    def __init__(self, max_samples: int = 1000) -> None:
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._timers: dict[str, deque] = {}
        self._max = max_samples

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            self._timers.setdefault(name, deque(maxlen=self._max)).append(float(seconds))

    def time(self, name: str):
        collector = self

        class _Timer:
            def __enter__(self_inner):
                self_inner.t = time.perf_counter()
                return self_inner

            def __exit__(self_inner, *exc):
                collector.observe(name, time.perf_counter() - self_inner.t)
                return False
        return _Timer()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timers = {}
            for name, samples in self._timers.items():
                ordered = sorted(samples)
                if ordered:
                    timers[name] = {"count": len(ordered), "min": ordered[0], "max": ordered[-1],
                                    "avg": sum(ordered) / len(ordered),
                                    "p95": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]}
            return {"counters": dict(self._counters), "gauges": dict(self._gauges), "timers": timers}


    def prometheus_text(self, prefix: str = "aica_registry") -> str:
        """Prometheus exposition format (text/plain; version=0.0.4) for dashboard scrapers."""
        snap = self.snapshot()
        lines: list[str] = []

        def metric(name: str) -> str:
            return prefix + "_" + "".join(c if c.isalnum() else "_" for c in name)
        for name, value in sorted(snap["counters"].items()):
            lines += [f"# TYPE {metric(name)} counter", f"{metric(name)} {value}"]
        for name, value in sorted(snap["gauges"].items()):
            lines += [f"# TYPE {metric(name)} gauge", f"{metric(name)} {value}"]
        for name, t in sorted(snap["timers"].items()):
            base = metric(name)
            lines += [f"# TYPE {base} summary", f'{base}{{quantile="0.95"}} {t["p95"]}', f"{base}_count {t['count']}",
                      f"{base}_sum {t['avg'] * t['count']}"]
        return "\n".join(lines) + "\n"


class StructuredLogger:
    """JSON-lines logger; secrets are removed before anything is stored or written."""

    def __init__(self, path: str | Path | None = None, *, max_memory: int = 500) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self.records: deque = deque(maxlen=max_memory)

    def log(self, level: str, message: str, **fields: Any) -> dict[str, Any]:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "level": str(level).upper(),
                  "message": str(message), **sanitize(fields)}
        with self._lock:
            self.records.append(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record


class EventBus:
    def __init__(self, *, history: int = 500, logger: StructuredLogger | None = None,
                 metrics: MetricsCollector | None = None) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, tuple[str, Callable[[dict[str, Any]], Any]]] = {}
        self._ids = itertools.count(1)
        self.recent: deque = deque(maxlen=history)
        self.logger = logger or StructuredLogger()
        self.metrics = metrics or MetricsCollector()
        self._reporting = False
        self._seq = itertools.count(1)

    def subscribe(self, pattern: str, callback: Callable[[dict[str, Any]], Any]) -> str:
        if not isinstance(pattern, str) or not pattern or not callable(callback):
            raise ValueError("pattern must be a non-empty string and callback callable")
        with self._lock:
            sub_id = f"sub_{next(self._ids)}"
            self._subs[sub_id] = (pattern, callback)
            return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            return self._subs.pop(sub_id, None) is not None

    def subscriptions(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"id": i, "pattern": p} for i, (p, _cb) in self._subs.items()]

    def emit(self, topic: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            event = {"seq": next(self._seq), "topic": str(topic), "ts": datetime.now(timezone.utc).isoformat(),
                     "payload": sanitize(payload or {})}
            self.recent.append(event)
            targets = [(i, cb) for i, (p, cb) in self._subs.items() if fnmatch.fnmatchcase(event["topic"], p)]
        self.metrics.incr("events.emitted")
        self.logger.log("INFO", "event", topic=event["topic"])
        delivered = 0
        for sub_id, callback in targets:
            try:
                callback(dict(event))
                delivered += 1
            except Exception as exc:  # isolate subscriber failures
                self.metrics.incr("events.subscriber_errors")
                self._report_error("subscriber_failed", exc, subscription=sub_id, topic=event["topic"])
        return {"delivered": delivered, "topic": event["topic"]}

    def _report_error(self, kind: str, exc: BaseException, **context: Any) -> None:
        """Automatic error reporting (guarded against recursion from failing error subscribers)."""
        self.logger.log("ERROR", kind, error_type=type(exc).__name__, **context)
        if self._reporting:
            return
        self._reporting = True
        try:
            self.emit("registry.error", {"kind": kind, "error_type": type(exc).__name__, **context})
        finally:
            self._reporting = False

    def since(self, seq: int, pattern: str = "*", limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self.recent if e["seq"] > seq and fnmatch.fnmatchcase(e["topic"], pattern)][:limit]

    def history(self, pattern: str = "*") -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self.recent if fnmatch.fnmatchcase(e["topic"], pattern)]


class AlertManager:
    """Fires notifier callbacks when >= threshold matching events occur within a time window."""

    def __init__(self, bus: EventBus, clock: Callable[[], float] = time.time) -> None:
        self.bus = bus
        self._clock = clock
        self._rules: dict[str, dict[str, Any]] = {}
        self._hits: dict[str, deque] = {}
        self.fired: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        bus.subscribe("*", self._on_event)

    def add_rule(self, name: str, pattern: str, *, threshold: int = 1, window_seconds: float = 60.0,
                 notifier: Callable[[dict[str, Any]], Any] | None = None) -> None:
        if threshold < 1 or window_seconds <= 0:
            raise ValueError("threshold>=1 and window_seconds>0 required")
        with self._lock:
            self._rules[name] = {"pattern": pattern, "threshold": threshold, "window": window_seconds, "notifier": notifier}
            self._hits[name] = deque()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event["topic"] == "registry.alert":
            return
        now = self._clock()
        with self._lock:
            rules = list(self._rules.items())
        for name, rule in rules:
            if not fnmatch.fnmatchcase(event["topic"], rule["pattern"]):
                continue
            with self._lock:
                hits = self._hits[name]
                hits.append(now)
                while hits and now - hits[0] > rule["window"]:
                    hits.popleft()
                trigger = len(hits) >= rule["threshold"]
                if trigger:
                    hits.clear()
            if trigger:
                alert = {"rule": name, "topic": event["topic"], "threshold": rule["threshold"]}
                self.fired.append(alert)
                if rule["notifier"] is not None:
                    try:
                        rule["notifier"](alert)
                    except Exception as exc:
                        self.bus._report_error("notifier_failed", exc, rule=name)
                self.bus.emit("registry.alert", alert)


class WebhookNotifier:
    """POSTs an alert as JSON to a webhook. HTTPS only unless loopback http is explicitly allowed (tests/local use)."""

    def __init__(self, url: str, *, timeout_seconds: float = 3.0, allow_loopback_http: bool = False) -> None:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback and allow_loopback_http):
            raise ValueError("webhook must be https (loopback http only when explicitly allowed)")
        if parsed.username or parsed.password:
            raise ValueError("credentials in webhook URL are not allowed")
        self.url, self.timeout = url, timeout_seconds
        self.sent = 0

    def __call__(self, alert: dict[str, Any]) -> None:
        import urllib.request
        body = json.dumps(sanitize(alert)).encode()
        request = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()
        self.sent += 1
