import tempfile
import unittest
from pathlib import Path

from projects.provider_store.local_model_provider_adapter import LocalModelProviderAdapter


class LocalModelProviderAdapterTests(unittest.TestCase):
    def test_valid_local_endpoint_and_successful_text_generation(self):
        calls = []
        def transport(url, payload, *, timeout_seconds):
            calls.append((url, payload, timeout_seconds))
            return {"response": "A grounded local model answer.", "done": True}

        adapter = LocalModelProviderAdapter(
            endpoint="http://127.0.0.1:11434/api/generate",
            model_id="qwen2.5:7b",
            timeout_seconds=15,
            transport=transport,
        )
        result = adapter.generate_text("Explain provider fallback.", system="Be concise.")
        self.assertEqual(result["status"], "LOCAL_TEXT_GENERATED")
        self.assertTrue(result["generated"])
        self.assertEqual(result["text"], "A grounded local model answer.")
        self.assertEqual(calls[0][0], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(calls[0][1]["model"], "qwen2.5:7b")
        self.assertEqual(calls[0][1]["system"], "Be concise.")
        self.assertEqual(calls[0][2], 15)

    def test_loopback_hosts_allowed_but_remote_hosts_and_other_paths_rejected(self):
        for endpoint in (
            "http://127.0.0.1:11434/api/generate",
            "http://[::1]:11434/api/generate",
            "https://127.0.0.1/api/generate",
        ):
            adapter = LocalModelProviderAdapter(endpoint=endpoint, model_id="local-model", transport=lambda *a, **k: {"response": "ok"})
            self.assertTrue(adapter.endpoint.startswith(("http://", "https://")))
        for endpoint in (
            "http://localhost:11434/api/generate",
            "https://example.com/api/generate",
            "http://192.168.1.20:11434/api/generate",
            "http://127.0.0.1/api/chat",
            "http://user:password@127.0.0.1:11434/api/generate",
            "http://127.0.0.1:11434/api/generate?redirect=https://example.com",
            "http://127.0.0.1:11434/api/generate#fragment",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    LocalModelProviderAdapter(endpoint=endpoint, model_id="local-model", transport=lambda *a, **k: {"response": "ok"})

    def test_invalid_model_and_timeout_are_rejected(self):
        for model in ("", "../secret", "bad model", None):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    LocalModelProviderAdapter(model_id=model, transport=lambda *a, **k: {"response": "ok"})
        for timeout in (0, 121, True, float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    LocalModelProviderAdapter(model_id="local-model", timeout_seconds=timeout, transport=lambda *a, **k: {"response": "ok"})

    def test_invalid_prompts_and_malformed_response_fail_closed(self):
        adapter = LocalModelProviderAdapter(model_id="local-model", transport=lambda *a, **k: {"response": ""})
        self.assertEqual(adapter.generate_text("  ")["status"], "INVALID_PROMPT")
        self.assertEqual(adapter.generate_text("valid")["status"], "INVALID_LOCAL_MODEL_RESPONSE")
        bad_system = adapter.generate_text("valid", system=[])
        self.assertEqual(bad_system["status"], "INVALID_SYSTEM_PROMPT")

    def test_incomplete_or_unmarked_response_is_rejected(self):
        for response in (
            {"response": "possibly truncated", "done": False},
            {"response": "missing completion marker"},
            {"response": "wrong marker", "done": "true"},
        ):
            with self.subTest(response=response):
                adapter = LocalModelProviderAdapter(
                    model_id="local-model",
                    transport=lambda *args, _response=response, **kwargs: _response,
                )
                result = adapter.generate_text("valid prompt")
                self.assertFalse(result["generated"])
                self.assertEqual(result["status"], "INVALID_LOCAL_MODEL_RESPONSE")

    def test_injected_transport_cannot_bypass_response_size_limit(self):
        oversized_text = "x" * (2 * 1024 * 1024 + 1)
        adapter = LocalModelProviderAdapter(
            model_id="local-model",
            transport=lambda *args, **kwargs: {"response": oversized_text},
        )
        result = adapter.generate_text("private prompt")
        self.assertEqual(result["status"], "LOCAL_MODEL_REQUEST_FAILED")
        self.assertFalse(result["generated"])
        self.assertEqual(result["error_type"], "ValueError")
        self.assertNotIn("private prompt", repr(result))

    def test_execute_matches_provider_service_contract_without_losing_unknown_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "local-output.txt"
            adapter = LocalModelProviderAdapter(
                model_id="local-model",
                transport=lambda *args, **kwargs: {"response": "local result", "done": True},
            )
            result = adapter.execute(
                prompt="Write a short line.", provider_id="local-ollama",
                request_id="req-1", output_path=output_path,
            )
            self.assertTrue(result["started"])
            self.assertTrue(result["success"])
            self.assertEqual(result["text"], "local result")
            self.assertEqual(result["request_id"], "req-1")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "local result\n")
            self.assertEqual(result["charge_outcome"], "not_incurred")

            invalid = adapter.execute(prompt="   ", output_path=output_path)
            self.assertFalse(invalid["started"])
            self.assertFalse(invalid["success"])

            broken = LocalModelProviderAdapter(
                model_id="local-model",
                transport=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
            ).execute(prompt="May have reached local runtime", output_path=output_path)
            self.assertTrue(broken["started"])
            self.assertFalse(broken["success"])
            self.assertEqual(broken["status"], "LOCAL_MODEL_REQUEST_FAILED")

    def test_transport_exceptions_do_not_leak_prompt_or_credentials(self):
        def broken_transport(*args, **kwargs):
            raise TimeoutError("secret prompt must not appear in returned error")

        adapter = LocalModelProviderAdapter(model_id="local-model", transport=broken_transport)
        result = adapter.generate_text("private prompt")
        self.assertEqual(result["status"], "LOCAL_MODEL_REQUEST_FAILED")
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertNotIn("private prompt", str(result))


if __name__ == "__main__":
    unittest.main()
