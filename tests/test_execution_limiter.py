import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from projects.provider_store.execution_limiter import ProviderExecutionLimiter


class ProviderExecutionLimiterTests(unittest.TestCase):
    def test_active_calls_never_exceed_provider_limit(self):
        limiter = ProviderExecutionLimiter({"image": 2}, max_waiting_per_provider=20)
        lock = threading.Lock()
        active = 0
        maximum = 0

        def operation():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return "ok"

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda _: limiter.run("image", operation, queue_timeout_seconds=2),
                range(30),
            ))
        self.assertTrue(all(item["acquired"] for item in results))
        self.assertLessEqual(maximum, 2)
        self.assertEqual(limiter.snapshot()["image"]["active"], 0)
        self.assertEqual(limiter.snapshot()["image"]["waiting"], 0)

    def test_overload_is_rejected_when_queue_is_full(self):
        limiter = ProviderExecutionLimiter({"image": 1}, max_waiting_per_provider=0)
        started = threading.Event()
        release = threading.Event()

        def operation():
            started.set()
            release.wait(2)
            return "done"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(limiter.run, "image", operation)
            self.assertTrue(started.wait(1))
            rejected = limiter.run("image", lambda: "should-not-run")
            self.assertEqual(rejected["status"], "EXECUTION_CAPACITY_REJECTED")
            self.assertFalse(rejected["acquired"])
            release.set()
            self.assertTrue(first.result(timeout=2)["acquired"])
        self.assertEqual(limiter.snapshot()["image"]["active"], 0)

    def test_waiting_request_acquires_released_slot(self):
        limiter = ProviderExecutionLimiter({"image": 1}, max_waiting_per_provider=1)
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(2)
            return "first"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(limiter.run, "image", slow)
            self.assertTrue(started.wait(1))
            second = pool.submit(
                limiter.run, "image", lambda: "second", queue_timeout_seconds=1
            )
            deadline = time.monotonic() + 1
            while limiter.snapshot()["image"]["waiting"] != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(limiter.snapshot()["image"]["waiting"], 1)
            release.set()
            self.assertEqual(first.result(timeout=2)["result"], "first")
            self.assertEqual(second.result(timeout=2)["result"], "second")
        self.assertEqual(limiter.snapshot()["image"]["active"], 0)
        self.assertEqual(limiter.snapshot()["image"]["waiting"], 0)

    def test_queue_timeout_and_cancellation_do_not_leak_waiter_slots(self):
        limiter = ProviderExecutionLimiter({"image": 1}, max_waiting_per_provider=2)
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(2)

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(limiter.run, "image", slow)
            self.assertTrue(started.wait(1))
            timed = limiter.run(
                "image", lambda: None, queue_timeout_seconds=0.02
            )
            self.assertEqual(timed["status"], "EXECUTION_QUEUE_TIMEOUT")
            cancel = threading.Event()
            waiting = pool.submit(
                limiter.run, "image", lambda: None,
                queue_timeout_seconds=1, cancel_event=cancel,
            )
            deadline = time.monotonic() + 1
            while limiter.snapshot()["image"]["waiting"] != 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            cancel.set()
            self.assertEqual(waiting.result(timeout=1)["status"], "EXECUTION_CANCELLED")
            release.set()
            first.result(timeout=2)
        self.assertEqual(limiter.snapshot()["image"]["waiting"], 0)
        self.assertEqual(limiter.snapshot()["image"]["active"], 0)

    def test_operation_exception_releases_permit(self):
        limiter = ProviderExecutionLimiter({"image": 1})
        with self.assertRaisesRegex(RuntimeError, "private failure"):
            limiter.run("image", lambda: (_ for _ in ()).throw(RuntimeError("private failure")))
        self.assertEqual(limiter.snapshot()["image"]["active"], 0)
        self.assertTrue(limiter.run("image", lambda: "recovered")["acquired"])

    def test_invalid_configuration_and_inputs_fail_closed(self):
        for limits in ({}, {"": 1}, {"image": 0}, {"image": True}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                ProviderExecutionLimiter(limits)
        with self.assertRaises(ValueError):
            ProviderExecutionLimiter({"image": 1}, max_waiting_per_provider=-1)
        limiter = ProviderExecutionLimiter({"image": 1})
        self.assertEqual(limiter.run("unknown", lambda: None)["status"], "PROVIDER_LIMIT_NOT_CONFIGURED")
        self.assertEqual(limiter.run("image", None)["status"], "INVALID_OPERATION")
        self.assertEqual(
            limiter.run("image", lambda: None, queue_timeout_seconds=float("nan"))["status"],
            "INVALID_QUEUE_TIMEOUT",
        )
        self.assertEqual(
            limiter.run("image", lambda: None, cancel_event=object())["status"],
            "INVALID_CANCEL_EVENT",
        )


if __name__ == "__main__":
    unittest.main()
