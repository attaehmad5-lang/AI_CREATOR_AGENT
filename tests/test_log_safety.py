import ast
import io
import json
import logging
import re
import unittest
from pathlib import Path

from projects.provider_store.log_safety import (
    JsonLogFormatter,
    SecretRedactionFilter,
    configure_provider_logging,
    get_provider_logger,
    redact_text,
    redact_value,
)


class LogSafetyTests(unittest.TestCase):
    def test_redacts_assignment_headers_known_tokens_and_urls(self):
        secret_values = [
            "secret-value-12345",
            "password-value-12345",
            "bearer-value-12345",
            "hf_abcdefghijklmnop",
            "ghp_abcdefghijklmnop",
            "github_pat_abcdefghijklmnop",
            "xoxb-12345678-abcdefgh",
            "sk-proj-abcdefghijklmnop",
            "jwt.payload.signature",
        ]
        message = (
            'api_key="secret-value-12345" password=password-value-12345 '
            "Authorization: Bearer bearer-value-12345 "
            "HF hf_abcdefghijklmnop GH ghp_abcdefghijklmnop "
            "PAT github_pat_abcdefghijklmnop Slack xoxb-12345678-abcdefgh "
            "OpenAI sk-proj-abcdefghijklmnop "
            "url=https://user:url-password-12345@example.invalid/path "
            "token=jwt.payload.signature"
        )
        cleaned = redact_text(message)
        for secret in secret_values:
            self.assertNotIn(secret, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_redacts_nested_sensitive_keys_without_mutating_input(self):
        original = {
            "provider": "sample",
            "access_token": "access-secret-123",
            "nested": [
                {"refresh_token": "refresh-secret-123", "healthy": True},
                {"api_key": "api-secret-123", "count": 4},
            ],
        }
        cleaned = redact_value(original)
        self.assertEqual(cleaned["access_token"], "[REDACTED]")
        self.assertEqual(cleaned["nested"][0]["refresh_token"], "[REDACTED]")
        self.assertEqual(cleaned["nested"][1]["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["nested"][1]["count"], 4)
        self.assertEqual(original["access_token"], "access-secret-123")

    def test_handles_cycles_bytes_depth_and_arbitrary_objects_safely(self):
        circular = {}
        circular["self"] = circular
        circular["payload"] = b"do-not-render"
        cleaned = redact_value(circular)
        self.assertEqual(cleaned["self"], "[CYCLE]")
        self.assertEqual(cleaned["payload"], "[REDACTED_BYTES]")
        self.assertEqual(redact_value(object()), "<object>")

    def test_log_filter_scrubs_message_extras_and_exception(self):
        stream = io.StringIO()
        logger = get_provider_logger("unit_test_filter")
        logger.handlers.clear()
        logger.filters.clear()
        logger.addFilter(SecretRedactionFilter())
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)
        try:
            try:
                raise RuntimeError('password="exception-secret-123" quote="x"\napi_key=exception-key-123')
            except RuntimeError:
                logger.exception(
                    "provider failed token=%s",
                    "message-secret-123",
                    extra={
                        "request_context": {
                            "authorization": "Bearer extra-secret-123",
                            "provider": "sample",
                        }
                    },
                )
        finally:
            logger.removeHandler(handler)
            handler.close()

        raw = stream.getvalue().strip()
        payload = json.loads(raw)
        for secret in (
            "exception-secret-123",
            "exception-key-123",
            "message-secret-123",
            "extra-secret-123",
        ):
            self.assertNotIn(secret, raw)
        self.assertEqual(payload["request_context"]["authorization"], "[REDACTED]")
        self.assertIn("[REDACTED]", payload["exception"])

    def test_json_formatter_keeps_escaped_exception_json_valid(self):
        formatter = JsonLogFormatter()
        try:
            raise ValueError('token="quoted-secret-123"\nnext line with password=hidden-secret-123')
        except ValueError:
            import sys
            record = logging.LogRecord("provider", logging.ERROR, "file.py", 1, "failed", None, sys.exc_info())
        rendered = formatter.format(record)
        payload = json.loads(rendered)
        self.assertNotIn("quoted-secret-123", rendered)
        self.assertNotIn("hidden-secret-123", rendered)
        self.assertTrue(payload["exception"])

    def test_configure_logging_is_idempotent_and_preserves_foreign_handlers(self):
        stream = io.StringIO()
        logger = get_provider_logger("unit_test_config")
        logger.handlers.clear()
        foreign = logging.NullHandler()
        logger.addHandler(foreign)
        try:
            configured = configure_provider_logging("unit_test_config", stream=stream)
            configure_provider_logging("unit_test_config", stream=stream)
            self.assertIs(configured, logger)
            self.assertIn(foreign, logger.handlers)
            owned = [h for h in logger.handlers if getattr(h, "_provider_store_managed", False)]
            self.assertEqual(len(owned), 1)
            logger.error("api_key=configure-secret-123")
            self.assertNotIn("configure-secret-123", stream.getvalue())
            self.assertIn("[REDACTED]", stream.getvalue())
        finally:
            for handler in tuple(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logger.filters.clear()
            logger.propagate = True

    def test_provider_application_source_has_no_raw_secret_logging_sinks(self):
        root = Path(__file__).resolve().parents[1]
        excluded_parts = {
            ".git", ".venv", "venv", "__pycache__", "build", "dist",
            "tests", "site-packages",
        }
        source_files = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            if any(part in excluded_parts or "backup" in part.lower() for part in relative.parts):
                continue
            if relative.parts[0] not in {"agents", "projects"} and len(relative.parts) > 1:
                continue
            source_files.append(path)

        unsafe = []
        sensitive_name = re.compile(
            r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
            r"password|passwd|credential|authorization|cookie|private[_-]?key)"
        )
        log_methods = {"debug", "info", "warning", "error", "critical", "exception", "log"}

        def dotted(node):
            if isinstance(node, ast.Name):
                return node.id
            if isinstance(node, ast.Attribute):
                prefix = dotted(node.value)
                return f"{prefix}.{node.attr}" if prefix else node.attr
            return ""

        def contains_unsanitized_sensitive_name(node):
            if isinstance(node, ast.Call):
                called = dotted(node.func).lower()
                if any(word in called for word in ("redact", "sanitize", "scrub")):
                    return False
            if isinstance(node, ast.Name) and sensitive_name.search(node.id):
                return True
            return any(contains_unsanitized_sensitive_name(child) for child in ast.iter_child_nodes(node))

        for path in source_files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                unsafe.append(f"{path}: source scan failed ({type(exc).__name__})")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = dotted(node.func)
                method = called.rsplit(".", 1)[-1].lower()
                owner = called.rsplit(".", 1)[0].lower() if "." in called else ""
                is_logger = method in log_methods and (
                    owner == "logging"
                    or any(part.endswith("logger") or part in {"logger", "log"} for part in owner.split("."))
                )
                is_stdout = (called == "print" or called.endswith(".stderr.write"))
                if not (is_logger or is_stdout):
                    continue
                if method == "exception" or any(
                    keyword.arg == "exc_info"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    unsafe.append(f"{path.relative_to(root)}:{node.lineno}: raw exception logging")
                    continue
                if any(contains_unsanitized_sensitive_name(arg) for arg in node.args):
                    unsafe.append(f"{path.relative_to(root)}:{node.lineno}: possible secret passed to {called}")
                for keyword in node.keywords:
                    if contains_unsanitized_sensitive_name(keyword.value):
                        unsafe.append(f"{path.relative_to(root)}:{node.lineno}: possible secret passed to {called}")

        self.assertEqual(unsafe, [], "Unsafe application logging sinks found:\n" + "\n".join(unsafe))


if __name__ == "__main__":
    unittest.main()
