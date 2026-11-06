import unittest

from projects.provider_store.provider_trace_manager import ProviderTraceManager, parse_traceparent


class ProviderTraceManagerTests(unittest.TestCase):
    def test_w3c_traceparent_is_validated_and_propagated_to_child_span(self):
        parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        parsed = parse_traceparent(parent)
        self.assertEqual(parsed["status"], "VALID")
        manager = ProviderTraceManager()
        child = manager.start_span("provider.call", traceparent=parent, attributes={"provider": "test"})
        self.assertEqual(child["status"], "SPAN_STARTED")
        self.assertEqual(child["trace_id"], "a" * 32)
        self.assertEqual(child["parent_span_id"], "b" * 16)
        self.assertTrue(child["traceparent"].startswith("00-" + "a" * 32 + "-"))

    def test_invalid_and_zero_trace_identifiers_fail_closed(self):
        self.assertEqual(parse_traceparent("bad")["status"], "INVALID_TRACEPARENT")
        self.assertEqual(parse_traceparent("00-" + "0" * 32 + "-" + "b" * 16 + "-01")["status"], "INVALID_TRACEPARENT")
        self.assertEqual(parse_traceparent("00-" + "a" * 32 + "-" + "0" * 16 + "-01")["status"], "INVALID_TRACEPARENT")
        manager = ProviderTraceManager()
        self.assertEqual(manager.start_span("call", traceparent="invalid")["status"], "INVALID_TRACEPARENT")
        self.assertEqual(manager.start_span("")["status"], "INVALID_SPAN_NAME")

    def test_span_attributes_redact_secrets_and_payloads(self):
        manager = ProviderTraceManager()
        span = manager.start_span("provider.call", attributes={
            "provider": "test", "api_key": "never-store", "prompt": "private prompt",
            "authorization_header": "Bearer never-store", "count": 2,
        })
        result = manager.finish_span(span, outcome="ERROR", duration_ms=12.5)
        self.assertEqual(result["status"], "SPAN_RECORDED")
        encoded = repr(result).casefold()
        self.assertNotIn("never-store", encoded)
        self.assertNotIn("private prompt", encoded)
        self.assertEqual(result["span"]["attributes"]["api_key"], "[REDACTED]")
        self.assertEqual(result["span"]["duration_ms"], 12.5)

    def test_span_history_is_bounded_and_trace_filtering_works(self):
        manager = ProviderTraceManager(max_spans=2)
        trace = manager.start_span("one")
        manager.finish_span(trace, duration_ms=1)
        trace_id = trace["trace_id"]
        for name in ("two", "three"):
            span = manager.start_span(name)
            manager.finish_span(span, duration_ms=1)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(manager.snapshot(trace_id=trace_id)["status"], "NO_TRACES")
        self.assertEqual(manager.snapshot(trace_id="invalid")["status"], "INVALID_TRACE_ID")

    def test_invalid_outcomes_and_durations_are_rejected(self):
        manager = ProviderTraceManager()
        span = manager.start_span("provider.call")
        self.assertEqual(manager.finish_span(span, outcome="UNKNOWN")["status"], "INVALID_SPAN_OUTCOME")
        self.assertEqual(manager.finish_span(span, duration_ms=-1)["status"], "INVALID_DURATION")
        self.assertEqual(manager.finish_span(span, duration_ms=float("nan"))["status"], "INVALID_DURATION")
        self.assertEqual(manager.finish_span(span, duration_ms=10 ** 1000)["status"], "INVALID_DURATION")
        self.assertEqual(manager.finish_span(None)["status"], "INVALID_SPAN")


if __name__ == "__main__":
    unittest.main()
