import unittest
from concurrent.futures import ThreadPoolExecutor

from projects.provider_store.retry_policy import CircuitBreaker, RetryPolicy


class RetryPolicyTests(unittest.TestCase):
    def test_retries_transient_server_failure_with_exponential_backoff(self):
        policy = RetryPolicy(max_attempts=4, base_delay_seconds=1, max_delay_seconds=10, jitter_fraction=0, random_fn=lambda: 0)
        first = policy.decide(attempt=1, status_code=503)
        second = policy.decide(attempt=2, status_code=503)
        self.assertTrue(first.retry)
        self.assertEqual(first.delay_seconds, 1)
        self.assertEqual(second.delay_seconds, 2)

    def test_http_429_respects_retry_after(self):
        policy = RetryPolicy(max_attempts=4, base_delay_seconds=1, jitter_fraction=0, random_fn=lambda: 0)
        result = policy.decide(attempt=1, status_code=429, retry_after=7)
        self.assertTrue(result.retry)
        self.assertEqual(result.delay_seconds, 7)
        self.assertEqual(result.reason, "RATE_LIMIT_RETRY_AFTER")

    def test_auth_and_nonretryable_errors_are_not_retried(self):
        policy = RetryPolicy()
        self.assertFalse(policy.decide(attempt=1, status_code=401).retry)
        self.assertFalse(policy.decide(attempt=1, status_code=400).retry)

    def test_timeout_is_retryable_but_respects_attempt_limit(self):
        policy = RetryPolicy(max_attempts=2, random_fn=lambda: 0)
        self.assertTrue(policy.decide(attempt=1, exception=TimeoutError()).retry)
        self.assertEqual(policy.decide(attempt=2, exception=TimeoutError()).reason, "RETRY_LIMIT_REACHED")

    def test_non_idempotent_request_is_not_retried(self):
        result = RetryPolicy().decide(attempt=1, status_code=503, idempotent=False)
        self.assertFalse(result.retry)
        self.assertEqual(result.reason, "NON_IDEMPOTENT_REQUEST")

    def test_retry_after_http_date_is_parsed(self):
        value = RetryPolicy._retry_after_seconds("Thu, 01 Jan 1970 00:00:00 GMT")
        self.assertEqual(value, 0)

    def test_circuit_breaker_opens_at_threshold_and_recovers_after_cooldown(self):
        now = [10.0]
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=5, clock=lambda: now[0])
        self.assertTrue(breaker.allow_request())
        breaker.record_failure()
        self.assertEqual(breaker.state, "CLOSED")
        breaker.record_failure()
        self.assertEqual(breaker.state, "OPEN")
        self.assertFalse(breaker.allow_request())
        now[0] += 5
        self.assertEqual(breaker.state, "HALF_OPEN")
        self.assertTrue(breaker.allow_request())
        self.assertFalse(breaker.allow_request(), "only one half-open probe may run at a time")
        breaker.record_success()
        self.assertEqual(breaker.state, "CLOSED")

    def test_circuit_breaker_admits_only_one_concurrent_half_open_probe(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=1, clock=lambda: now[0])
        breaker.record_failure()
        now[0] = 1.0
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: breaker.allow_request(), range(40)))
        self.assertEqual(sum(results), 1)

    def test_circuit_breaker_reopens_after_half_open_failure(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=2, clock=lambda: now[0])
        breaker.record_failure()
        now[0] += 2
        self.assertEqual(breaker.state, "HALF_OPEN")
        breaker.record_failure()
        self.assertEqual(breaker.state, "OPEN")

    def test_invalid_policy_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=0)
        with self.assertRaises(ValueError):
            RetryPolicy(max_attempts=True)
        with self.assertRaises(ValueError):
            RetryPolicy(base_delay_seconds=float("nan"))
        with self.assertRaises(ValueError):
            RetryPolicy(max_delay_seconds=float("inf"))
        with self.assertRaises(ValueError):
            RetryPolicy(jitter_fraction=float("nan"))
        with self.assertRaises(ValueError):
            RetryPolicy(random_fn="not-callable")
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=0)
        with self.assertRaises(ValueError):
            CircuitBreaker(failure_threshold=True)
        with self.assertRaises(ValueError):
            CircuitBreaker(cooldown_seconds=float("nan"))
        with self.assertRaises(ValueError):
            CircuitBreaker(cooldown_seconds=float("inf"))
        with self.assertRaises(ValueError):
            CircuitBreaker(clock="not-callable")

    def test_invalid_attempt_and_non_finite_retry_after_are_safe(self):
        policy = RetryPolicy(random_fn=lambda: 0)
        self.assertEqual(policy.decide(attempt=0, status_code=503).reason, "INVALID_ATTEMPT")
        self.assertEqual(policy.decide(attempt=True, status_code=503).reason, "INVALID_ATTEMPT")
        self.assertIsNone(policy._retry_after_seconds(float("inf")))
        self.assertIsNone(policy._retry_after_seconds(float("nan")))


if __name__ == "__main__":
    unittest.main()
