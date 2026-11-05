"""Strict W3C Trace Context and request-ID correlation tests."""
import unittest

from projects.provider_store.trace_context import TraceContext, TraceContextError


class TraceContextTests(unittest.TestCase):
    def test_generates_unique_valid_trace_and_span_ids(self):
        first = TraceContext.create("request-1")
        second = TraceContext.create("request-2")
        for context in (first, second):
            self.assertRegex(context.trace_id, r"^[0-9a-f]{32}$")
            self.assertRegex(context.span_id, r"^[0-9a-f]{16}$")
            self.assertNotEqual(context.trace_id, "0" * 32)
            self.assertNotEqual(context.span_id, "0" * 16)
            self.assertEqual(context.trace_flags, "00")
        self.assertNotEqual(first.trace_id, second.trace_id)
        self.assertNotEqual(first.span_id, second.span_id)

    def test_generates_unique_request_ids_when_missing(self):
        first = TraceContext.create()
        second = TraceContext.create()
        self.assertRegex(first.request_id, r"^req-[0-9a-f]{32}$")
        self.assertNotEqual(first.request_id, second.request_id)

    def test_incoming_traceparent_creates_child_span_and_preserves_trace_flags(self):
        parent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-03"
        context = TraceContext.create("req-1", traceparent=parent)
        self.assertEqual(context.trace_id, "0123456789abcdef0123456789abcdef")
        self.assertEqual(context.parent_span_id, "0123456789abcdef")
        self.assertNotEqual(context.span_id, context.parent_span_id)
        self.assertEqual(context.trace_flags, "03")
        self.assertNotEqual(context.traceparent, parent)

    def test_explicit_trace_id_must_match_incoming_traceparent(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        parent = f"00-{trace_id}-0123456789abcdef-01"
        self.assertEqual(
            TraceContext.create("req-1", trace_id=trace_id, traceparent=parent).trace_id,
            trace_id,
        )
        with self.assertRaises(TraceContextError):
            TraceContext.create(
                "req-1", trace_id="1" * 32,
                traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            )

    def test_invalid_request_ids_are_rejected(self):
        for value in ("", "has space", "line\nbreak", "x" * 201, 123, b"request"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TraceContextError):
                    TraceContext.create(value)

    def test_invalid_explicit_trace_ids_are_rejected(self):
        for value in ("", "0" * 32, "A" * 32, "g" * 32, "1" * 31, "1" * 33, 123):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TraceContextError):
                    TraceContext.create("req-1", trace_id=value)

    def test_invalid_traceparent_shapes_zero_ids_and_versions_are_rejected(self):
        bad_values = (
            123, "", "00-short",
            "00-" + "0" * 32 + "-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-" + "0" * 16 + "-01",
            "ff-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0g",
            "00-0123456789ABCDEF0123456789ABCDEF-0123456789abcdef-01",
            "00-0123456789abcdef0123456789abcdef-0123456789ABCDEF-01",
        )
        for value in bad_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(TraceContextError):
                    TraceContext.create("req-1", traceparent=value)

    def test_trace_headers_contain_only_correlation_fields(self):
        context = TraceContext.create("req-1")
        self.assertEqual(context.headers["x-request-id"], "req-1")
        self.assertEqual(context.headers["traceparent"], context.traceparent)
        self.assertEqual(set(context.headers), {"x-request-id", "traceparent"})
        self.assertEqual(context.as_dict()["trace_id"], context.trace_id)
        self.assertNotIn("authorization", repr(context.headers).casefold())
        self.assertNotIn("token", repr(context.as_dict()).casefold())

    def test_context_is_immutable(self):
        context = TraceContext.create("req-1")
        with self.assertRaises((AttributeError, TypeError)):
            context.trace_id = "1" * 32

    def test_incoming_flags_are_preserved_without_forcing_sampling(self):
        unsampled = TraceContext.create(
            "req-1",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
        )
        sampled = TraceContext.create(
            "req-2",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        )
        self.assertTrue(unsampled.traceparent.endswith("-00"))
        self.assertTrue(sampled.traceparent.endswith("-01"))


if __name__ == "__main__":
    unittest.main()
