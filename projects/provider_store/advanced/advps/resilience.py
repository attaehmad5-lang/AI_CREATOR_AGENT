"""Retry with budget, rate-limit handling, and a thread-safe circuit breaker."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import AmbiguousOutcomeError, CircuitOpenError, ProviderError, RateLimitError


class RetryBudget:
    """Token bucket limiting retries so a failing provider cannot trigger a retry storm."""

    def __init__(self, capacity: float = 10.0, refill_per_success: float = 0.1):
        self.capacity = capacity
        self.refill = refill_per_success
        self._tokens = capacity
        self._lock = threading.Lock()

    def try_spend(self) -> bool:
        with self._lock:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def on_success(self) -> None:
        with self._lock:
            self._tokens = min(self.capacity, self._tokens + self.refill)

    @property
    def tokens(self) -> float:
        with self._lock:
            return self._tokens


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: float = 0.1


def retry_call(
    fn: Callable[[], object],
    policy: RetryPolicy = RetryPolicy(),
    budget: Optional[RetryBudget] = None,
    idempotent: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
):
    """Call fn with retries. Ambiguous outcomes are only retried when the call is idempotent."""
    if policy.max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    attempt = 0
    while True:
        attempt += 1
        try:
            result = fn()
            if budget:
                budget.on_success()
            return result
        except AmbiguousOutcomeError:
            if not idempotent or attempt >= policy.max_attempts:
                raise
            delay = None
        except ProviderError as exc:
            if not exc.retryable or attempt >= policy.max_attempts:
                raise
            delay = exc.retry_after if isinstance(exc, RateLimitError) else None
        if budget and not budget.try_spend():
            raise ProviderError("retry budget exhausted", retryable=False)
        if delay is None:
            delay = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
            delay += delay * policy.jitter * rng()
        sleep(min(delay, policy.max_delay))


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int = 5, reset_timeout: float = 30.0, clock: Callable[[], float] = time.monotonic):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._clock = clock
        self._state = self.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._peek()

    def _peek(self) -> str:
        if self._state == self.OPEN and self._clock() - self._opened_at >= self.reset_timeout:
            return self.HALF_OPEN
        return self._state

    def allow(self) -> None:
        with self._lock:
            state = self._peek()
            if state == self.OPEN:
                raise CircuitOpenError("circuit open")
            if state == self.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpenError("half-open probe in flight")
                self._state = self.HALF_OPEN
                self._probe_in_flight = True

    def record_success(self) -> None:
        with self._lock:
            self._state, self._failures, self._probe_in_flight = self.CLOSED, 0, False

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state == self.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state = self.OPEN
                self._opened_at = self._clock()
