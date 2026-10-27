import io
import json
import logging
import unittest
from datetime import datetime, timezone

from projects.provider_store.provider_structured_logger import ProviderStructuredLogger


class ProviderStructuredLoggerTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = logging.Logger("provider-store-test", level=logging.DEBUG)
        self.logger.addHandler(logging.StreamHandler(self.stream))
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.events = ProviderStructuredLogger(self.logger, clock=lambda: self.now)

    def test_json_event_contains_only_explicit_safe_fields(self):
        result = self.events.emit(
            "provider.verify",
            provider_id="image-provider",
            status="AUTH_FAILED",
            outcome="ERROR",
            duration_ms=12.5,
            request_id="job-123",
            category="image",
            error_code="HTTP_401",
        )
        self.assertTrue(result["logged"])
        record = json.loads(self.stream.getvalue())
        self.assertEqual(record["event"], "provider.verify")
        self.assertEqual(record["provider_id"], "image-provider")
        self.assertEqual(record["duration_ms"], 12.5)
        self.assertEqual(record["error_code"], "HTTP_401")
        self.assertNotIn("message", record)
        self.assertNotIn("payload", record)

    def test_invalid_fields_and_sensitive_input_never_enter_log(self):
        result = self.events.emit(
            "provider.verify",
            provider_id="provider",
            status="ERROR",
            outcome="ERROR",
            error_code="token=top-secret",
        )
        self.assertTrue(result["logged"])
        encoded = self.stream.getvalue()
        self.assertNotIn("top-secret", encoded)
        self.assertNotIn("token=", encoded)
        self.assertNotIn("token=top-secret", encoded)

    def test_invalid_event_status_duration_and_clock_fail_closed(self):
        self.assertEqual(self.events.emit("../secret", status="OK", outcome="OK")["status"], "INVALID_LOG_EVENT")
        self.assertEqual(self.events.emit("provider.call", status="bad status", outcome="OK")["status"], "INVALID_LOG_STATUS")
        for value in (-1, True, float("nan"), float("inf"), 10**1000):
            with self.subTest(value=value):
                self.assertEqual(
                    self.events.emit("provider.call", status="OK", outcome="OK", duration_ms=value)["status"],
                    "INVALID_LOG_DURATION",
                )
        invalid_clock = ProviderStructuredLogger(self.logger, clock=lambda: "not-time")
        self.assertEqual(
            invalid_clock.emit("provider.call", status="OK", outcome="OK")["status"],
            "INVALID_LOG_CLOCK",
        )


if __name__ == "__main__":
    unittest.main()
