import re
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from projects.provider_store.provider_request_trace import (
    activate_request_context,
    child_span,
    create_request_context,
    current_request_context,
    current_trace_context,
    extract_trace_context,
    generate_request_id,
    inject_trace_headers,
    parse_traceparent,
    validate_request_id,
    trace_provider_request,
)


class ProviderRequestTraceTests(unittest.TestCase):
    def test_generated_request_ids_are_unique_uuid4_values(self):
        values = {generate_request_id() for _ in range(500)}
        self.assertEqual(len(values), 500)
        for value in values:
            self.assertEqual(str(uuid.UUID(value)).lower(), value)
            self.assertTrue(validate_request_id(value))

    def test_request_id_validation_is_bounded_and_safe(self):
        self.assertTrue(validate_request_id("request-123"))
        self.assertTrue(validate_request_id("req_123:part.2"))
        self.assertFalse(validate_request_id(""))
        self.assertFalse(validate_request_id("bad request"))
        self.assertFalse(validate_request_id("x" * 201))
        self.assertFalse(validate_request_id("Bearer secret"))

    def test_w3c_traceparent_round_trip_and_parent_relationship(self):
        remote = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        trace_id, parent_span, flags = parse_traceparent(remote)
        self.assertEqual(trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(parent_span, "00f067aa0ba902b7")
        self.assertEqual(flags, "01")
        context = create_request_context(request_id="request-1", traceparent=remote)
        self.assertEqual(context.trace_id, trace_id)
        self.assertEqual(context.parent_span_id, parent_span)
        self.assertRegex(context.span_id, r"^[0-9a-f]{16}$")
        self.assertEqual(context.traceparent[:35], remote[:35])

    def test_invalid_zero_ids_flags_and_conflicting_trace_id_are_rejected(self):
        for value in (
            "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
            "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "not-a-traceparent",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_traceparent(value)
        with self.assertRaisesRegex(ValueError, "TRACE_ID_CONFLICT"):
            create_request_context(
                trace_id="1" * 32,
                traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            )
        with self.assertRaisesRegex(ValueError, "INVALID_TRACE_ID"):
            create_request_context(trace_id="0" * 32)

    def test_context_is_scoped_and_child_span_restores_parent(self):
        context = create_request_context(request_id="request-2")
        self.assertIsNone(current_request_context())
        with activate_request_context(context):
            parent = current_trace_context()
            with child_span() as child:
                self.assertEqual(child.trace_id, context.trace_id)
                self.assertEqual(child.parent_span_id, context.span_id)
                self.assertNotEqual(child.span_id, context.span_id)
            self.assertEqual(current_trace_context(), parent)
        self.assertIsNone(current_request_context())

    def test_trace_headers_are_copied_and_injected_case_insensitively(self):
        context = create_request_context(request_id="request-3")
        original = {"X-Test": "safe", "TraceParent": "old-value"}
        with activate_request_context(context):
            injected = inject_trace_headers(original)
        self.assertEqual(original["TraceParent"], "old-value")
        self.assertEqual(injected["X-Test"], "safe")
        self.assertEqual(injected["traceparent"], context.traceparent)
        self.assertNotIn("TraceParent", injected)
        extracted = extract_trace_context({"TRACEPARENT": context.traceparent})
        self.assertEqual(extracted.trace_id, context.trace_id)
        self.assertEqual(extracted.parent_span_id, context.span_id)
        with self.assertRaisesRegex(ValueError, "TRACEPARENT_REQUIRED"):
            extract_trace_context({})

    def test_decorator_generates_ids_and_restores_context_after_exception(self):
        observed = {}

        @trace_provider_request
        def handler(*, request_id=None, trace_id=None, traceparent=None):
            observed.update(current_trace_context())
            return {"status": "OK"}

        result = handler()
        self.assertEqual(result["status"], "OK")
        self.assertTrue(validate_request_id(result["request_id"]))
        self.assertEqual(result["trace_id"], observed["trace_id"])
        self.assertEqual(result["traceparent"], observed["traceparent"])
        self.assertIsNone(current_request_context())

        @trace_provider_request
        def broken(*, request_id=None, trace_id=None, traceparent=None):
            raise RuntimeError("expected")

        with self.assertRaises(RuntimeError):
            broken()
        self.assertIsNone(current_request_context())

    def test_decorator_rejects_malformed_incoming_context(self):
        @trace_provider_request
        def handler(*, request_id=None, trace_id=None, traceparent=None):
            self.fail("handler must not run")

        result = handler(traceparent="invalid")
        self.assertEqual(result["status"], "INVALID_TRACEPARENT")
        self.assertFalse(result["executed"])

    def test_contexts_are_isolated_between_threads(self):
        contexts = [create_request_context(request_id=f"thread-{i}") for i in range(8)]

        def read(index):
            with activate_request_context(contexts[index]):
                return current_trace_context()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(8)))
        self.assertEqual({item["trace_id"] for item in results}, {c.trace_id for c in contexts})
        self.assertTrue(all(item["request_id"] == f"thread-{i}" for i, item in enumerate(results)))
        self.assertIsNone(current_request_context())


if __name__ == "__main__":
    unittest.main()
