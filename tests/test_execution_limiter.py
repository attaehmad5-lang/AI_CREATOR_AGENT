import threading
import time
import unittest

from projects.provider_store.execution_limiter import ProviderExecutionLimiter


class ProviderExecutionLimiterTests(unittest.TestCase):
    def test_global_and_per_provider_limits_reject_overload(self):
        limiter = ProviderExecutionLimiter(
            max_concurrent=2, max_pending=1, per_provider_limits={"paid": 1}
        )
        first = limiter.acquire("paid", "req-1")
        second_same_provider = limiter.acquire("paid", "req-2")
        other_provider = limiter.acquire("free", "req-3")
        self.assertTrue(first["acquired"])
        self.assertEqual(second_same_provider["status"], "OVERLOADED")
        self.assertTrue(other_provider["acquired"])
        self.assertEqual(limiter.acquire("free", "req-4")["status"], "OVERLOADED")
        self.assertEqual(limiter.summary()["active"], 2)
        self.assertEqual(limiter.release("paid", "req-1")["status"], "RELEASED")
        self.assertEqual(limiter.release("free", "req-3")["status"], "RELEASED")

    def test_waiting_request_acquires_slot_after_release(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1, max_pending=2)
        self.assertTrue(limiter.acquire("image", "active")["acquired"])
        outcome = []
        started = threading.Event()
        def waiter():
            started.set()
            outcome.append(limiter.acquire("image", "waiting", timeout_seconds=2))
        thread = threading.Thread(target=waiter)
        thread.start()
        started.wait(1)
        deadline = time.monotonic() + 1
        while limiter.summary()["pending"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(limiter.summary()["pending"], 1)
        limiter.release("image", "active")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(outcome[0]["acquired"])
        self.assertGreaterEqual(outcome[0]["wait_ms"], 0)
        limiter.release("image", "waiting")

    def test_pending_queue_is_bounded_and_provider_fifo_is_preserved(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1, max_pending=1)
        limiter.acquire("image", "active")
        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(limiter.acquire("image", "queued", timeout_seconds=2))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while limiter.summary()["pending"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        full = limiter.acquire("image", "overflow", timeout_seconds=1)
        self.assertEqual(full["status"], "QUEUE_FULL")
        limiter.release("image", "active")
        thread.join(2)
        self.assertTrue(outcome[0]["acquired"])
        limiter.release("image", "queued")

    def test_queue_timeout_removes_waiter_and_cancellation_is_supported(self):
        limiter = ProviderExecutionLimiter(max_concurrent=1, max_pending=2)
        limiter.acquire("image", "active")
        timeout_result = limiter.acquire("image", "timeout", timeout_seconds=0.02)
        self.assertEqual(timeout_result["status"], "QUEUE_TIMEOUT")
        self.assertEqual(limiter.summary()["pending"], 0)

        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(limiter.acquire("image", "cancel-me", timeout_seconds=2))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while limiter.summary()["pending"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(limiter.cancel_pending("cancel-me")["status"], "CANCELLED")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0]["status"], "CANCELLED")
        limiter.release("image", "active")

    def test_duplicate_request_ids_and_invalid_releases_fail_closed(self):
        limiter = ProviderExecutionLimiter(max_concurrent=2)
        self.assertTrue(limiter.acquire("image", "same-request")["acquired"])
        self.assertEqual(limiter.acquire("video", "same-request")["status"], "DUPLICATE_REQUEST")
        self.assertEqual(limiter.release("video", "same-request")["status"], "PROVIDER_MISMATCH")
        self.assertEqual(limiter.release("image", "same-request")["status"], "RELEASED")
        self.assertEqual(limiter.release("image", "same-request")["status"], "NOT_ACTIVE")
        self.assertEqual(limiter.acquire("../bad", "req")["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(limiter.acquire("image", "bad id")["status"], "INVALID_REQUEST_ID")
        self.assertEqual(limiter.acquire("image", "req", timeout_seconds=float("nan"))["status"], "INVALID_WAIT_TIMEOUT")

    def test_other_provider_can_progress_when_one_provider_is_at_its_limit(self):
        limiter = ProviderExecutionLimiter(
            max_concurrent=2, max_pending=2, per_provider_limits={"slow": 1}
        )
        limiter.acquire("slow", "slow-1")
        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(limiter.acquire("slow", "slow-2", timeout_seconds=2))
        )
        thread.start()
        deadline = time.monotonic() + 1
        while limiter.summary()["pending"] != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        other = limiter.acquire("fast", "fast-1")
        self.assertTrue(other["acquired"])
        limiter.release("fast", "fast-1")
        limiter.release("slow", "slow-1")
        thread.join(2)
        self.assertTrue(outcome[0]["acquired"])
        limiter.release("slow", "slow-2")

    def test_configuration_limits_are_validated(self):
        for kwargs in (
            {"max_concurrent": 0},
            {"max_pending": -1},
            {"per_provider_limits": {"image": 0}},
            {"per_provider_limits": {"image": 9}, "max_concurrent": 8},
            {"per_provider_limits": []},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProviderExecutionLimiter(**kwargs)


if __name__ == "__main__":
    unittest.main()
