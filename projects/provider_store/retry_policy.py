"""Deterministic retry decisions and provider circuit-breaker primitives."""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from .safe_numbers import finite_number


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    attempt: int
    delay_seconds: float
    reason: str


class RetryPolicy:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 30.0,
        jitter_fraction: float = 0.2,
        random_fn: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        normalized_limits = tuple(finite_number(value, minimum=0) for value in (
            base_delay_seconds, max_delay_seconds, jitter_fraction
        ))
        if any(value is None for value in normalized_limits):
            raise ValueError("retry limits must be finite numbers")
        base_delay_seconds, max_delay_seconds, jitter_fraction = normalized_limits
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("invalid backoff limits")
        if not 0 <= jitter_fraction <= 1:
            raise ValueError("jitter_fraction must be between 0 and 1")
        if random_fn is not None and not callable(random_fn):
            raise ValueError("random_fn must be callable")
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.jitter_fraction = jitter_fraction
        self.random_fn = random_fn or random.random

    @staticmethod
    def _retry_after_seconds(value: str | int | float | None) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if not isinstance(value, (str, int, float)):
            return None
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (OverflowError, TypeError, ValueError):
            pass
        try:
            target = parsedate_to_datetime(str(value))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    def decide(
        self,
        *,
        attempt: int,
        status_code: int | None = None,
        exception: BaseException | None = None,
        retry_after: str | int | float | None = None,
        idempotent: bool = True,
    ) -> RetryDecision:
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            return RetryDecision(False, attempt if isinstance(attempt, int) else 0, 0.0, "INVALID_ATTEMPT")
        if attempt >= self.max_attempts:
            return RetryDecision(False, attempt, 0.0, "RETRY_LIMIT_REACHED")
        if not isinstance(idempotent, bool):
            return RetryDecision(False, attempt, 0.0, "INVALID_IDEMPOTENCY_FLAG")
        if exception is not None:
            if status_code is None:
                try:
                    candidate_status = getattr(exception, "status_code", None)
                except Exception:
                    candidate_status = None
                if (
                    isinstance(candidate_status, int)
                    and not isinstance(candidate_status, bool)
                    and 100 <= candidate_status <= 599
                ):
                    status_code = candidate_status
            if retry_after is None:
                try:
                    candidate_retry_after = getattr(exception, "retry_after", None)
                except Exception:
                    candidate_retry_after = None
                if isinstance(candidate_retry_after, (str, int, float)) and not isinstance(candidate_retry_after, bool):
                    retry_after = candidate_retry_after
        if status_code is not None and (
            not isinstance(status_code, int) or isinstance(status_code, bool)
            or status_code < 100 or status_code > 599
        ):
            return RetryDecision(False, attempt, 0.0, "INVALID_STATUS_CODE")
        if not idempotent:
            return RetryDecision(False, attempt, 0.0, "NON_IDEMPOTENT_REQUEST")
        if status_code in {401, 403}:
            return RetryDecision(False, attempt, 0.0, "AUTHORIZATION_FAILURE")
        retryable_status = status_code == 429 or (status_code is not None and 500 <= status_code <= 599)
        retryable_exception = isinstance(exception, (TimeoutError, ConnectionError, OSError))
        if not retryable_status and not retryable_exception:
            return RetryDecision(False, attempt, 0.0, "NON_RETRYABLE_FAILURE")
        exponent = attempt - 1
        if self.base_delay_seconds == 0 or self.max_delay_seconds == 0:
            exponential = 0.0
        else:
            # Cap before exponentiation. A malformed/extreme attempt number must not
            # allocate a gigantic integer or overflow while computing backoff.
            cap_exponent = max(
                0,
                math.ceil(
                    math.log2(self.max_delay_seconds)
                    - math.log2(self.base_delay_seconds)
                ),
            )
            if exponent >= cap_exponent:
                exponential = self.max_delay_seconds
            else:
                try:
                    exponential = min(
                        self.max_delay_seconds,
                        math.ldexp(self.base_delay_seconds, exponent),
                    )
                except OverflowError:
                    exponential = self.max_delay_seconds
        try:
            random_value = self.random_fn()
            if (
                not isinstance(random_value, (int, float))
                or isinstance(random_value, bool)
                or not math.isfinite(random_value)
            ):
                return RetryDecision(False, attempt, 0.0, "INVALID_JITTER_SOURCE")
            jitter = exponential * self.jitter_fraction * min(1.0, max(0.0, float(random_value)))
        except Exception:
            return RetryDecision(False, attempt, 0.0, "INVALID_JITTER_SOURCE")
        delay = min(self.max_delay_seconds, exponential + jitter)
        header_delay = self._retry_after_seconds(retry_after)
        if status_code == 429:
            if header_delay is not None:
                # Never let an untrusted Retry-After value bypass the configured backoff cap.
                delay = min(self.max_delay_seconds, max(delay, header_delay))
                reason = "RATE_LIMIT_RETRY_AFTER" if header_delay else "RATE_LIMIT_BACKOFF"
            else:
                reason = "RATE_LIMIT_BACKOFF"
        else:
            reason = "TRANSIENT_HTTP_FAILURE" if retryable_status else "TRANSIENT_NETWORK_FAILURE"
        return RetryDecision(True, attempt, delay, reason)


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(failure_threshold, int) or isinstance(failure_threshold, bool) or failure_threshold < 1:
            raise ValueError("failure_threshold must be a positive integer")
        cooldown_value = finite_number(cooldown_seconds, minimum=0)
        if cooldown_value is None:
            raise ValueError("cooldown_seconds must be a finite non-negative number")
        cooldown_seconds = cooldown_value
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        import time
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock or time.monotonic
        self.failures = 0
        self.opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._lock = threading.Lock()

    def _state_unlocked(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            return "HALF_OPEN"
        return "OPEN"

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    def allow_request(self) -> bool:
        with self._lock:
            current = self._state_unlocked()
            if current == "CLOSED":
                return True
            if current == "HALF_OPEN" and not self._half_open_probe_in_flight:
                self._half_open_probe_in_flight = True
                return True
            return False

    def cancel_probe(self) -> None:
        """Release a half-open probe slot when execution was cancelled before an outcome."""
        with self._lock:
            self._half_open_probe_in_flight = False

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self._half_open_probe_in_flight = False

    def record_failure(self) -> str:
        with self._lock:
            was_half_open = self._state_unlocked() == "HALF_OPEN"
            self.failures += 1
            if was_half_open or (self.failures >= self.failure_threshold and self.opened_at is None):
                self.opened_at = self.clock()
            self._half_open_probe_in_flight = False
            return self._state_unlocked()
