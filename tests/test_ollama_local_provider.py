"""Loopback-only Ollama adapter tests using an in-process HTTP server."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from projects.provider_store.ollama_local_provider import OllamaLocalProvider


class OllamaTestHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        mode = self.server.mode
        if mode == "http_error":
            return self._send(503, b'{"error":"offline"}')
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/api/tags")
            self.end_headers()
            return
        if mode == "oversized":
            return self._send(200, b'{"models":[' + b'"model-x",' * 300 + b"]}")
        if mode == "bad_json":
            return self._send(200, b"{")
        if mode == "wrong_content_type":
            return self._send(200, b'{"models":[]}', "text/html")
        if mode == "bad_catalog":
            return self._send(200, b'{"models":[{"name":7}]}')
        if mode == "slow":
            import time
            time.sleep(0.3)
        return self._send(200, json.dumps({
            "models": [{"name": "llama3:8b"}, {"name": "qwen2.5:7b"}]
        }).encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            self.server.last_payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self.server.last_payload = None
        self.server.post_count += 1
        mode = self.server.mode
        if mode == "bad_generation":
            return self._send(200, b'{"response":123,"done":true}')
        if mode == "not_done":
            return self._send(200, b'{"response":"partial","done":false}')
        if mode == "oversized_generation":
            return self._send(200, json.dumps({
                "response": "x" * 3000, "done": True,
            }).encode("utf-8"))
        return self._send(200, b'{"response":"local answer","done":true}')


class OllamaLocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaTestHandler)
        self.server.mode = "normal"
        self.server.post_count = 0
        self.server.last_payload = None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.provider = OllamaLocalProvider(self.endpoint)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_discovery_and_health_check_report_real_local_models(self):
        result = self.provider.discover_models()
        self.assertEqual(result["status"], "LOCAL_MODELS_AVAILABLE")
        self.assertEqual(result["model_count"], 2)
        self.assertIn("llama3:8b", result["models"])
        health = self.provider.health_check()
        self.assertTrue(health["healthy"])
        self.assertEqual(health["model_count"], 2)

    def test_generation_uses_discovered_local_model_and_disables_streaming(self):
        self.provider.discover_models()
        result = self.provider.generate_text(
            model="llama3:8b", prompt="Write a short outline.",
        )
        self.assertEqual(result["status"], "LOCAL_GENERATION_COMPLETED")
        self.assertEqual(result["text"], "local answer")
        self.assertFalse(result["provider_call_external"])
        self.assertEqual(self.server.last_payload["model"], "llama3:8b")
        self.assertFalse(self.server.last_payload["stream"])
        self.assertEqual(self.server.last_payload["prompt"], "Write a short outline.")

    def test_generation_is_blocked_until_model_is_discovered(self):
        result = self.provider.generate_text(model="llama3:8b", prompt="hello")
        self.assertEqual(result["status"], "MODEL_NOT_DISCOVERED")
        self.assertEqual(self.server.post_count, 0)

    def test_invalid_discovery_clears_stale_model_allowlist(self):
        self.provider.discover_models()
        self.server.mode = "bad_catalog"
        result = self.provider.discover_models()
        self.assertEqual(result["status"], "INVALID_LOCAL_MODEL_CATALOG")
        blocked = self.provider.generate_text(model="llama3:8b", prompt="hello")
        self.assertEqual(blocked["status"], "MODEL_NOT_DISCOVERED")
        self.assertEqual(self.server.post_count, 0)

    def test_non_loopback_or_unsafe_endpoints_are_rejected(self):
        for endpoint in (
            "https://localhost:11434",
            "http://example.com:11434",
            "http://127.0.0.1:0",
            "http://user@127.0.0.1:11434",
            "http://127.0.0.1:11434/private",
            "http://127.0.0.1:11434?token=secret",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    OllamaLocalProvider(endpoint)

    def test_bounded_timeout_and_size_configuration(self):
        for kwargs in (
            {"timeout_seconds": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("inf")},
            {"max_response_bytes": 100},
            {"max_prompt_chars": True},
            {"max_output_chars": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    OllamaLocalProvider(self.endpoint, **kwargs)

    def test_http_errors_redirects_and_invalid_json_fail_closed(self):
        for mode, expected in (
            ("http_error", "LOCAL_HTTP_ERROR"),
            ("redirect", "LOCAL_HTTP_ERROR"),
            ("bad_json", "INVALID_LOCAL_RESPONSE"),
            ("wrong_content_type", "INVALID_LOCAL_RESPONSE"),
        ):
            with self.subTest(mode=mode):
                self.server.mode = mode
                result = self.provider.discover_models()
                self.assertEqual(result["status"], expected)

    def test_oversized_local_response_is_rejected(self):
        self.server.mode = "oversized"
        provider = OllamaLocalProvider(self.endpoint, max_response_bytes=1024)
        result = provider.discover_models()
        self.assertEqual(result["status"], "LOCAL_RESPONSE_TOO_LARGE")

    def test_generation_rejects_malformed_or_incomplete_local_responses(self):
        self.provider.discover_models()
        for mode in ("bad_generation", "not_done"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                result = self.provider.generate_text(model="llama3:8b", prompt="hello")
                self.assertEqual(result["status"], "INVALID_LOCAL_GENERATION_RESPONSE")

    def test_generation_response_size_is_bounded(self):
        self.provider.discover_models()
        self.server.mode = "oversized_generation"
        provider = OllamaLocalProvider(
            self.endpoint, max_response_bytes=1024, max_output_chars=3000,
        )
        provider.discover_models()
        result = provider.generate_text(model="llama3:8b", prompt="hello")
        self.assertEqual(result["status"], "LOCAL_RESPONSE_TOO_LARGE")

    def test_invalid_models_and_prompts_are_rejected_without_http_post(self):
        self.provider.discover_models()
        self.assertEqual(
            self.provider.generate_text(model="../bad", prompt="hello")["status"],
            "INVALID_MODEL",
        )
        self.assertEqual(
            self.provider.generate_text(model="llama3:8b", prompt="  ")["status"],
            "INVALID_PROMPT",
        )
        limited = OllamaLocalProvider(self.endpoint, max_prompt_chars=3)
        limited.discover_models()
        self.assertEqual(
            limited.generate_text(model="llama3:8b", prompt="four")["status"],
            "INVALID_PROMPT",
        )
        self.assertEqual(self.server.post_count, 0)

    def test_connection_failures_are_reported_without_cloud_fallback(self):
        self.server.shutdown()
        result = self.provider.discover_models()
        self.assertEqual(result["status"], "LOCAL_CONNECTION_FAILED")
        self.assertIsNone(result["models"])


if __name__ == "__main__":
    unittest.main()
