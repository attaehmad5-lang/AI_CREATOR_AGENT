import unittest

from projects.provider_store.trace_context import TraceContext, TraceContextError


class TraceContextTests(unittest.TestCase):
    def test_generates_valid_unique_trace_and_span_ids(self):
        first = TraceContext.create("request-1")
        second = TraceContext.create("request-2")
        self.assertRegex(first.trace_id, r"^[0-9a-f]{32}$")
        self.assertRegex(first.span_id, r"^[0-9a-f]{16}$")
        self.assertNotEqual(first.trace_id, "0" * 32)
        self.assertNotEqual(first.span_id, "0" * 16)
        self.assertNotEqual(first.trace_id, second.trace_id)
        self.assertNotEqual(first.span_id, second.span_id)

    def test_missing_request_id_generates_unique_request_ids(self):
        first = TraceContext.create(None)
        second = TraceContext.create(None)
        self.assertRegex(first.request_id, r"^req-[0-9a-f]{32}$")
        self.assertNotEqual(first.request_id, second.request_id)

    def test_traceparent_has_w3c_v00_shape(self):
        context = TraceContext.create("req-1")
        self.assertRegex(context.traceparent, r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")

    def test_incoming_traceparent_preserves_trace_and_parent_span(self):
        parent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-03"
        context = TraceContext.create("req-1", traceparent=parent)
        self.assertEqual(context.trace_id, "0123456789abcdef0123456789abcdef")
        self.assertEqual(context.parent_span_id, "0123456789abcdef")
        self.assertNotEqual(context.span_id, context.parent_span_id)
        self.assertEqual(context.trace_flags, "03")

    def test_explicit_trace_id_matches_incoming_traceparent(self):
        trace_id = "0123456789abcdef0123456789abcdef"
        parent = f"00-{trace_id}-0123456789abcdef-01"
        context = TraceContext.create("req-1", trace_id=trace_id, traceparent=parent)
        self.assertEqual(context.trace_id, trace_id)

    def test_conflicting_trace_id_and_parent_are_rejected(self):
        with self.assertRaises(TraceContextError):
            TraceContext.create(
                "req-1",
                trace_id="11111111111111111111111111111111",
                traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            )

    def test_invalid_request_ids_are_rejected(self):
        for value in ("", "has space", "line\nbreak", "x" * 201, 123):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TraceContextError):
                    TraceContext.create(value)

    def test_invalid_explicit_trace_ids_are_rejected(self):
        for value in ("", "0" * 32, "A" * 32, "g" * 32, "1" * 31, "1" * 33, None):
            if value is None:
                continue
            with self.subTest(value=value):
                with self.assertRaises(TraceContextError):
                    TraceContext.create("req-1", trace_id=value)

    def test_invalid_traceparent_types_and_shapes_are_rejected(self):
        for value in (123, "", "00-short", "00-" + "0" * 32 + "-0123456789abcdef-01",
                      "00-0123456789abcdef0123456789abcdef-" + "0" * 16 + "-01",
                      "ff-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
                      "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
                      "00-0123456789abcdef0123456789abcdef-0123456789abcdef-0g"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(TraceContextError):
                    TraceContext.create("req-1", traceparent=value)

    def test_none_traceparent_is_allowed(self):
        context = TraceContext.create("req-1", traceparent=None)
        self.assertIsNone(context.parent_span_id)

    def test_traceparent_flags_are_preserved(self):
        context = TraceContext.create(
            "req-1",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-00",
        )
        self.assertEqual(context.trace_flags, "00")
        self.assertTrue(context.traceparent.endswith("-00"))

    def test_headers_include_request_and_trace_context(self):
        context = TraceContext.create("req-1")
        self.assertEqual(context.headers["x-request-id"], "req-1")
        self.assertEqual(context.headers["traceparent"], context.traceparent)
        self.assertEqual(set(context.headers), {"x-request-id", "traceparent"})

    def test_as_dict_contains_correlation_fields_only(self):
        context = TraceContext.create("req-1")
        value = context.as_dict()
        self.assertEqual(value["request_id"], "req-1")
        self.assertEqual(value["trace_id"], context.trace_id)
        self.assertEqual(value["span_id"], context.span_id)
        self.assertIsNone(value["parent_span_id"])
        self.assertNotIn("token", repr(value).casefold())
        self.assertNotIn("authorization", repr(value).casefold())

    def test_context_is_immutable(self):
        context = TraceContext.create("req-1")
        with self.assertRaises((AttributeError, TypeError)):
            context.trace_id = "1" * 32

    def test_request_ids_allow_safe_correlation_characters(self):
        context = TraceContext.create("job:part_1.v2-123")
        self.assertEqual(context.request_id, "job:part_1.v2-123")

    def test_invalid_traceparent_is_never_silently_replaced(self):
        with self.assertRaises(TraceContextError):
            TraceContext.create("req-1", traceparent="invalid")
