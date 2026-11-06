"""Unit tests for stable request IDs, W3C Trace Context and provider propagation."""
import re
import unittest

from projects.provider_store.provider_trace_context import (
    ProviderRequestTraceContext,
    current_provider_trace_context,
    with_provider_trace_context,
)


class ProviderTraceContextTests(unittest.TestCase):
    def test_generates_unique_request_trace_and_span_ids(self):
        first = ProviderRequestTraceContext.create()
        second = ProviderRequestTraceContext.create()
        self.assertRegex(first.request_id, r"^[0-9a-f]{32}$")
        self.assertRegex(first.trace_id, r"^[0-9a-f]{32}$")
        self.assertRegex(first.span_id, r"^[0-9a-f]{16}$")
        self.assertEqual(first.traceparent, f"00-{first.trace_id}-{first.span_id}-00")
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertNotEqual(first.trace_id, second.trace_id)
        self.assertNotEqual(first.span_id, second.span_id)

    def test_accepts_w3c_parent_and_creates_child_span(self):
        parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        context = ProviderRequestTraceContext.create(
            request_id="incoming-request-1", traceparent=parent,
        )
        self.assertEqual(context.request_id, "incoming-request-1")
        self.assertEqual(context.trace_id, "a" * 32)
        self.assertEqual(context.parent_span_id, "b" * 16)
        self.assertNotEqual(context.span_id, "b" * 16)
        self.assertEqual(context.trace_flags, "01")
        self.assertEqual(
            context.traceparent,
            "00-" + "a" * 32 + "-" + context.span_id + "-01",
        )

    def test_nested_child_keeps_trace_and_sets_parent_span(self):
        parent = ProviderRequestTraceContext.create(request_id="parent-request")
        child = parent.child()
        grandchild = child.child()
        self.assertEqual(child.trace_id, parent.trace_id)
        self.assertEqual(grandchild.trace_id, parent.trace_id)
        self.assertEqual(child.parent_span_id, parent.span_id)
        self.assertEqual(grandchild.parent_span_id, child.span_id)
        self.assertNotEqual(parent.span_id, child.span_id)
        self.assertNotEqual(child.span_id, grandchild.span_id)

    def test_rejects_malformed_and_zero_w3c_context(self):
        invalid = [
            "traceparent",
            "00-" + "0" * 32 + "-" + "b" * 16 + "-00",
            "00-" + "a" * 32 + "-" + "0" * 16 + "-00",
            "01-" + "a" * 32 + "-" + "b" * 16 + "-00",
            "00-" + "A" * 32 + "-" + "b" * 16 + "-00",
            "00-" + "a" * 32 + "-" + "b" * 16 + "-0g",
        ]
        for traceparent in invalid:
            with self.subTest(traceparent=traceparent):
                with self.assertRaises(ValueError):
                    ProviderRequestTraceContext.create(traceparent=traceparent)

    def test_rejects_control_character_and_oversized_request_ids(self):
        for request_id in ("bad\nrequest", "request with spaces", "x" * 201):
            with self.subTest(request_id=request_id):
                with self.assertRaises(ValueError):
                    ProviderRequestTraceContext.create(request_id=request_id)

    def test_context_variable_restores_after_wrapped_call(self):
        class Target:
            @with_provider_trace_context
            def invoke(self, *, request_id=None, traceparent=None, _trace_context=None):
                active = current_provider_trace_context()
                assert active is _trace_context
                return {"status": "OK", "inside_trace": active.trace_id}

        target = Target()
        result = target.invoke(request_id="trace-test-1")
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["trace_id"], result["inside_trace"])
        self.assertIsNone(current_provider_trace_context())

    def test_decorator_rejects_invalid_trace_before_wrapped_call(self):
        called = []

        class Target:
            @with_provider_trace_context
            def invoke(self, *, request_id=None, traceparent=None, _trace_context=None):
                called.append(True)
                return {"status": "OK"}

        result = Target().invoke(
            request_id="trace-test-2",
            traceparent="00-" + "0" * 32 + "-" + "b" * 16 + "-00",
        )
        self.assertEqual(result["status"], "INVALID_TRACEPARENT")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
