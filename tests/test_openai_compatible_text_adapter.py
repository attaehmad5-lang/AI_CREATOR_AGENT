import json
import unittest
from urllib.error import HTTPError, URLError

from projects.provider_store.openai_compatible_text_adapter import OpenAICompatibleTextAdapter


class _Response:
    def __init__(self, payload=None, status=200, raw=None):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.read_sizes = []
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.raw[:size] if size >= 0 else self.raw

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response or _Response({
            "choices": [{"message": {"content": "generated text"}}]
        })
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class OpenAICompatibleTextAdapterTests(unittest.TestCase):
    def test_loopback_http_and_allowlisted_https_endpoints_are_accepted(self):
        valid = [
            ("http://localhost:11434/v1/chat/completions", None),
            ("http://127.0.0.1:11434/v1/chat/completions", None),
            ("http://[::1]:11434/v1/chat/completions", None),
            ("https://api.example.com/v1/chat/completions", ["api.example.com"]),
        ]
        for endpoint, hosts in valid:
            with self.subTest(endpoint=endpoint):
                adapter = OpenAICompatibleTextAdapter(
                    endpoint=endpoint, model="local-model", allowed_hosts=hosts
                )
                self.assertEqual(adapter.endpoint, endpoint)

    def test_50_endpoint_security_cases_fail_closed(self):
        bad_endpoints = [
            "http://api.example.com/v1/chat/completions",
            "http://192.0.2.10/v1/chat/completions",
            "http://localhost.evil.example/v1/chat/completions",
            "https://evil.example.com/v1/chat/completions",
            "https://user:pass@api.example.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions#fragment",
            "https://api.example.com/v1/chat/completions?token=secret",
            "ftp://api.example.com/v1/chat/completions",
            "https://api.example.com",
            "https://api.example.com/",
            "https://api.example.com:99999/v1/chat/completions",
            "https://api.example.com/v1/chat/completions" + "x" * 2100,
            "",
            None,
            123,
            "http://0.0.0.0:11434/v1/chat/completions",
            "http://[::]:11434/v1/chat/completions",
            "http://10.0.0.5:11434/v1/chat/completions",
            "http://172.16.0.2:11434/v1/chat/completions",
            "http://192.168.1.2:11434/v1/chat/completions",
            "http://example.local/v1/chat/completions",
            "http://169.254.169.254/v1/chat/completions",
            "https://api.example.com:0/v1/chat/completions",
            "https://api.example.com:abc/v1/chat/completions",
            "https://api.example.com/v1/chat/completions\n",
        ]
        bad_endpoints = bad_endpoints + bad_endpoints[:25]
        checks = 0
        for endpoint in bad_endpoints:
            with self.subTest(index=checks, endpoint=endpoint):
                with self.assertRaises((ValueError, TypeError)):
                    OpenAICompatibleTextAdapter(
                        endpoint=endpoint,
                        model="model",
                        allowed_hosts=["api.example.com"],
                    )
                checks += 1
        self.assertEqual(checks, 50)

    def test_generation_posts_chat_payload_and_returns_only_content(self):
        opener = _Opener()
        adapter = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions",
            model="qwen2.5",
            opener=opener,
            timeout_seconds=4.5,
        )
        result = adapter.generate_text("Write a scene", system_prompt="You are a film writer")
        self.assertEqual(result, "generated text")
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 4.5)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen2.5")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "You are a film writer"})
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "Write a scene"})
        self.assertFalse(payload["stream"])
        self.assertTrue(opener.response.closed)

    def test_environment_credential_is_used_but_never_returned(self):
        secret = "private-test-key"
        opener = _Opener()
        adapter = OpenAICompatibleTextAdapter(
            endpoint="https://api.example.com/v1/chat/completions",
            model="text-model",
            allowed_hosts=["api.example.com"],
            api_key_env="TEXT_API_KEY",
            environ={"TEXT_API_KEY": secret},
            opener=opener,
        )
        result = adapter.generate_text("hello")
        self.assertEqual(result, "generated text")
        request, _ = opener.calls[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, repr(result))

    def test_missing_credential_fails_before_network(self):
        opener = _Opener()
        adapter = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions",
            model="local-model",
            api_key_env="TEXT_API_KEY",
            environ={},
            opener=opener,
        )
        with self.assertRaises(PermissionError):
            adapter.generate_text("hello")
        self.assertEqual(opener.calls, [])

    def test_response_schema_and_size_errors_are_rejected(self):
        invalid_payloads = [
            {},
            {"choices": []},
            {"choices": [{}]},
            {"choices": [{"message": {}}]},
            {"choices": [{"message": {"content": 5}}]},
            [],
            None,
            {"choices": [{"message": {"content": None}}]},
            {"choices": "invalid"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                adapter = OpenAICompatibleTextAdapter(
                    endpoint="http://localhost:11434/v1/chat/completions",
                    model="local-model",
                    opener=_Opener(_Response(payload=payload)),
                )
                with self.assertRaises(ValueError):
                    adapter.generate_text("hello")
        oversized = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions",
            model="local-model",
            opener=_Opener(_Response(raw=b"x" * 200)),
            max_response_bytes=100,
        )
        with self.assertRaises(ValueError):
            oversized.generate_text("hello")

    def test_http_auth_rate_limit_redirect_and_network_failures_are_sanitized(self):
        cases = [
            (401, PermissionError),
            (403, PermissionError),
            (429, ConnectionError),
            (302, ConnectionError),
            (500, ConnectionError),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                error = HTTPError("https://api.example.com", status, "private", {}, None)
                adapter = OpenAICompatibleTextAdapter(
                    endpoint="https://api.example.com/v1/chat/completions",
                    model="text-model",
                    allowed_hosts=["api.example.com"],
                    opener=_Opener(error=error),
                )
                with self.assertRaises(expected) as caught:
                    adapter.generate_text("hello")
                self.assertNotIn("private", str(caught.exception))
        timeout_adapter = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions",
            model="local-model",
            opener=_Opener(error=URLError(TimeoutError("private timeout detail"))),
        )
        with self.assertRaises(TimeoutError) as caught:
            timeout_adapter.generate_text("hello")
        self.assertNotIn("private timeout detail", str(caught.exception))

    def test_invalid_generation_parameters_are_rejected(self):
        adapter = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions", model="local-model"
        )
        cases = [
            {"prompt": ""},
            {"prompt": None},
            {"prompt": "hello", "temperature": float("nan")},
            {"prompt": "hello", "temperature": float("inf")},
            {"prompt": "hello", "temperature": -0.1},
            {"prompt": "hello", "temperature": 2.1},
            {"prompt": "hello", "max_tokens": 0},
            {"prompt": "hello", "max_tokens": True},
            {"prompt": "hello", "max_tokens": 32769},
            {"prompt": "hello", "system_prompt": 9},
        ]
        for options in cases:
            with self.subTest(options=options):
                prompt = options.pop("prompt")
                with self.assertRaises(ValueError):
                    adapter.generate_text(prompt, **options)

    def test_adapter_constructor_rejects_invalid_model_credentials_and_limits(self):
        cases = [
            {"model": ""},
            {"model": "../secret"},
            {"model": "x" * 201},
            {"api_key_env": "lowercase"},
            {"api_key_env": "KEY=VALUE"},
            {"timeout_seconds": 0},
            {"timeout_seconds": 301},
            {"timeout_seconds": float("nan")},
            {"max_response_bytes": 0},
            {"max_response_bytes": 20_000_001},
        ]
        for options in cases:
            with self.subTest(options=options):
                args = {"endpoint": "http://localhost:11434/v1/chat/completions", "model": "local"}
                args.update(options)
                with self.assertRaises(ValueError):
                    OpenAICompatibleTextAdapter(**args)

    def test_adapter_declares_versioned_text_contract(self):
        adapter = OpenAICompatibleTextAdapter(
            endpoint="http://localhost:11434/v1/chat/completions",
            model="local-model",
        )
        self.assertEqual(adapter.API_VERSION, "1.0")
        self.assertEqual(adapter.adapter_api_version, "1.0")
        self.assertTrue(callable(adapter.generate_text))


if __name__ == "__main__":
    unittest.main()
