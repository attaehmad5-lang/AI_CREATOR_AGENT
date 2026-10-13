import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from projects.generation.providers.pollinations_provider import PollinationsProvider
from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.http_probe import ProviderHTTPStatusError


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Response:
    def __init__(self, body=PNG, status=200, content_type="image/png"):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.closed = False
        self.offset = 0

    def getcode(self):
        return self.status

    def read(self, size=-1):
        if size < 0:
            size = len(self.body)
        data = self.body[self.offset:self.offset + size]
        self.offset += len(data)
        return data

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response or _Response()
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class PollinationsProviderTests(unittest.TestCase):
    def test_generation_uses_documented_https_endpoint_bearer_auth_and_atomic_output(self):
        secret = "private-pollinations-key"
        opener = _Opener(_Response())
        provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": secret},
            opener=opener,
            timeout_seconds=12,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.png"
            result = provider.generate_image(
                "cinematic\nwide landscape", output, model="flux",
                width=1280, height=720, seed=42,
            )
            request, timeout = opener.calls[0]
            self.assertEqual(request.full_url.split("?", 1)[0],
                             "https://gen.pollinations.ai/image/cinematic%0Awide%20landscape")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
            self.assertEqual(timeout, 12)
            self.assertIn("width=1280", request.full_url)
            self.assertIn("height=720", request.full_url)
            self.assertIn("seed=42", request.full_url)
            self.assertEqual(output.read_bytes(), PNG)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["content_type"], "image/png")
            self.assertNotIn(secret, repr(result))
            self.assertTrue(opener.response.closed)

    def test_missing_or_malformed_api_key_fails_before_network(self):
        opener = _Opener()
        provider = PollinationsProvider(environ={}, opener=opener)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(PermissionError, "POLLINATIONS_API_KEY is missing"):
                provider.generate_image("test", Path(temporary) / "image.png")
        provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "secret\r\nX-Evil: injected"},
            opener=opener,
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(PermissionError, "format is invalid"):
                provider.generate_image("test", Path(temporary) / "image.png")
        self.assertEqual(opener.calls, [])

    def test_http_errors_are_safe_and_keep_retry_after_metadata(self):
        error = HTTPError(
            "https://gen.pollinations.ai/image/test", 429, "Too Many Requests",
            {"Retry-After": "7"}, None,
        )
        opener = _Opener(error=error)
        provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"}, opener=opener
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ProviderHTTPStatusError) as caught:
                provider.generate_image("test", Path(temporary) / "image.png")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, "7")
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_timeout_and_network_errors_are_classified_without_details(self):
        timeout_provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"},
            opener=_Opener(error=URLError(TimeoutError("private network detail"))),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(TimeoutError) as caught:
                timeout_provider.generate_image("test", Path(temporary) / "image.png")
        self.assertNotIn("private network detail", str(caught.exception))

        network_provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"},
            opener=_Opener(error=URLError("private host details")),
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ConnectionError, "unreachable") as caught:
                network_provider.generate_image("test", Path(temporary) / "image.png")
        self.assertNotIn("private host details", str(caught.exception))

    def test_oversized_or_non_image_response_is_rejected_and_not_saved(self):
        oversized = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"},
            opener=_Opener(_Response(body=PNG + b"x" * 2048)),
            max_output_bytes=1024,
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.png"
            with self.assertRaisesRegex(ValueError, "size limit"):
                oversized.generate_image("test", output)
            self.assertFalse(output.exists())

        invalid_response = _Response(body=b"not an image", content_type="text/plain")
        invalid = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"},
            opener=_Opener(invalid_response),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.png"
            with self.assertRaisesRegex(ValueError, "supported image artifact"):
                invalid.generate_image("test", output)
            self.assertFalse(output.exists())
            self.assertTrue(invalid_response.closed)

    def test_prompt_model_dimensions_and_seed_are_validated(self):
        provider = PollinationsProvider(
            environ={"POLLINATIONS_API_KEY": "private"}, opener=_Opener()
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.png"
            invalid_calls = [
                ("", {}),
                ("test", {"model": "../secret"}),
                ("test", {"width": True}),
                ("test", {"height": 10000}),
                ("test", {"seed": -1}),
                ("x" * 20001, {}),
            ]
            for prompt, options in invalid_calls:
                with self.subTest(options=options, prompt_length=len(prompt)):
                    with self.assertRaises(ValueError):
                        provider.generate_image(prompt, output, **options)
        self.assertEqual(provider.opener.calls, [])

    def test_pollinations_adapter_loads_through_central_provider_contract(self):
        repository_root = Path(__file__).resolve().parents[1]
        catalog_path = repository_root / "projects" / "provider_store" / "provider_catalog.json"
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "connection-state.json"
            state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            adapter = PollinationsProvider(
                environ={"POLLINATIONS_API_KEY": "private"},
                opener=_Opener(_Response()),
            )
            loaded = manager.load_provider("pollinations", lambda metadata: adapter)
            self.assertEqual(loaded["status"], "LOADED")
            self.assertTrue(manager.validate_provider_contract("pollinations", adapter)["valid"])
            self.assertIs(manager.get_loaded_provider("pollinations"), adapter)
            unloaded = manager.unload_provider("pollinations")
            self.assertEqual(unloaded["status"], "UNLOADED")

    def test_provider_configuration_is_bounded(self):
        with self.assertRaises(ValueError):
            PollinationsProvider(timeout_seconds=float("inf"))
        with self.assertRaises(ValueError):
            PollinationsProvider(max_output_bytes=1)
        with self.assertRaises(ValueError):
            PollinationsProvider(opener=object())


if __name__ == "__main__":
    unittest.main()
