import threading
import unittest

from projects.provider_store.streaming_response_manager import ProviderStreamSession


class ProviderStreamSessionTests(unittest.TestCase):
    def test_ordered_chunks_finish_with_digest_and_payload(self):
        stream = ProviderStreamSession("request-1")
        self.assertEqual(stream.append(sequence=0, chunk="hello")["status"], "CHUNK_ACCEPTED")
        self.assertEqual(stream.append(sequence=1, chunk=b" world")["status"], "CHUNK_ACCEPTED")
        result = stream.finish()
        self.assertEqual(result["status"], "STREAM_COMPLETED")
        self.assertEqual(result["content"], b"hello world")
        self.assertEqual(result["chunk_count"], 2)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(stream.snapshot()["state"], "COMPLETED")

    def test_duplicate_chunks_are_idempotent_but_conflicts_and_gaps_reject(self):
        stream = ProviderStreamSession("request-2")
        stream.append(sequence=0, chunk=b"first")
        self.assertEqual(
            stream.append(sequence=0, chunk=b"first")["status"], "DUPLICATE_CHUNK"
        )
        self.assertEqual(
            stream.append(sequence=0, chunk=b"changed")["status"], "SEQUENCE_CONFLICT"
        )
        gap = stream.append(sequence=2, chunk=b"third")
        self.assertEqual(gap["status"], "SEQUENCE_GAP")
        self.assertEqual(gap["expected_sequence"], 1)

    def test_chunk_and_byte_limits_fail_closed(self):
        by_count = ProviderStreamSession("request-3", max_chunks=1)
        by_count.append(sequence=0, chunk=b"a")
        self.assertEqual(
            by_count.append(sequence=1, chunk=b"b")["status"], "CHUNK_LIMIT_EXCEEDED"
        )
        self.assertEqual(by_count.snapshot()["state"], "LIMIT_EXCEEDED")

        by_size = ProviderStreamSession("request-4", max_bytes=3)
        self.assertEqual(by_size.append(sequence=0, chunk=b"abc")["status"], "CHUNK_ACCEPTED")
        self.assertEqual(
            by_size.append(sequence=1, chunk=b"d")["status"], "BYTE_LIMIT_EXCEEDED"
        )

    def test_timeout_uses_injected_monotonic_clock_and_heartbeat(self):
        now = [10.0]
        stream = ProviderStreamSession(
            "request-5", timeout_seconds=5, clock=lambda: now[0]
        )
        now[0] = 14.0
        self.assertEqual(stream.heartbeat()["status"], "HEARTBEAT_ACCEPTED")
        now[0] = 20.0
        self.assertEqual(stream.append(sequence=0, chunk=b"late")["status"], "STREAM_TIMED_OUT")
        self.assertEqual(stream.snapshot()["state"], "TIMED_OUT")

    def test_cancel_discards_buffer_and_terminal_states_reject_writes(self):
        stream = ProviderStreamSession("request-6")
        stream.append(sequence=0, chunk=b"secret output")
        self.assertTrue(stream.cancel(reason="caller stopped")["cancelled"])
        self.assertEqual(stream.snapshot()["total_bytes"], 0)
        self.assertEqual(stream.append(sequence=0, chunk=b"late")["status"], "STREAM_NOT_OPEN")

    def test_invalid_inputs_and_constructor_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderStreamSession("")
        with self.assertRaises(ValueError):
            ProviderStreamSession("request", max_chunks=True)
        stream = ProviderStreamSession("request-7")
        self.assertEqual(stream.append(sequence=True, chunk=b"x")["status"], "INVALID_SEQUENCE")
        self.assertEqual(stream.append(sequence=0, chunk=object())["status"], "INVALID_CHUNK")
        self.assertEqual(stream.cancel(reason="")["status"], "INVALID_CANCEL_REASON")

    def test_non_finite_timeout_clock_and_malformed_utf8_are_rejected(self):
        for timeout in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    ProviderStreamSession("request", timeout_seconds=timeout)
        for clock_value in (float("nan"), float("inf"), True, "not-a-clock"):
            with self.subTest(clock_value=clock_value):
                with self.assertRaises(ValueError):
                    ProviderStreamSession("request", clock=lambda value=clock_value: value)
        stream = ProviderStreamSession("request-valid")
        self.assertEqual(
            stream.append(sequence=0, chunk="\\ud800")["status"],
            "INVALID_CHUNK",
        )
        self.assertEqual(
            ProviderStreamSession("bad\\nrequest").snapshot()["state"],
            "OPEN",
        )

    def test_concurrent_duplicate_chunk_delivery_is_thread_safe(self):
        stream = ProviderStreamSession("request-8")
        barrier = threading.Barrier(12)
        results = []
        result_lock = threading.Lock()

        def submit():
            barrier.wait()
            result = stream.append(sequence=0, chunk=b"same")
            with result_lock:
                results.append(result["status"])

        workers = [threading.Thread(target=submit) for _ in range(12)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(results.count("CHUNK_ACCEPTED"), 1)
        self.assertEqual(results.count("DUPLICATE_CHUNK"), 11)


if __name__ == "__main__":
    unittest.main()
