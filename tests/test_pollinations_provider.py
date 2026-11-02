import base64
import tempfile
import unittest
from pathlib import Path

from projects.generation.providers.pollinations_provider import PollinationsProvider


PNG = bytes.fromhex("89504e470d0a1a0a") + b"test-image-bytes"
JPEG = bytes.fromhex("ffd8ff") + b"test-jpeg-bytes"


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class PollinationsProviderTests(unittest.TestCase):
    def test_sends_bearer_key_and_writes_png_with_matching_extension(self):
        response = FakeResponse({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        session = FakeSession(response)
        provider = PollinationsProvider(
            session=session,
            environ={"POLLINATIONS_API_KEY": "test-secret"},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "image.png"
            result = provider.generate_image("cinematic landscape", output)
            self.assertEqual(output.read_bytes(), PNG)
            self.assertEqual(result["output"], str(output))
            self.assertEqual(result["format"], "png")
        args, kwargs = session.calls[0]
        self.assertEqual(args[0], provider.endpoint)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(kwargs["json"]["model"], provider.default_model)
        self.assertEqual(kwargs["json"]["response_format"], "b64_json")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("test-secret", repr(result))
        self.assertTrue(response.closed)

    def test_jpeg_bytes_get_jpg_extension_instead_of_png(self):
        response = FakeResponse({"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})
        provider = PollinationsProvider(
            session=FakeSession(response),
            environ={"POLLINATIONS_API_KEY": "test-secret"},
        )
        with tempfile.TemporaryDirectory() as directory:
            requested = Path(directory) / "image.png"
            result = provider.generate_image("cinematic landscape", requested)
            actual = Path(result["output"])
            self.assertEqual(actual.suffix, ".jpg")
            self.assertFalse(requested.exists())
            self.assertEqual(actual.read_bytes(), JPEG)
            self.assertEqual(result["format"], "jpg")

    def test_missing_key_fails_before_network_call(self):
        session = FakeSession(FakeResponse({}))
        provider = PollinationsProvider(session=session, environ={})
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            provider.generate_image("prompt", "unused.png")
        self.assertEqual(session.calls, [])

    def test_402_reports_budget_exhaustion_without_response_body(self):
        response = FakeResponse({"error": "private detail"}, status_code=402)
        provider = PollinationsProvider(
            session=FakeSession(response),
            environ={"POLLINATIONS_API_KEY": "secret"},
        )
        with self.assertRaisesRegex(RuntimeError, "budget is exhausted") as caught:
            provider.generate_image("prompt", "unused.png")
        self.assertNotIn("private detail", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(response.closed)

    def test_rejects_invalid_base64_and_non_image_payload(self):
        for encoded in ("%%%", base64.b64encode(b"not-an-image").decode()):
            response = FakeResponse({"data": [{"b64_json": encoded}]})
            provider = PollinationsProvider(
                session=FakeSession(response),
                environ={"POLLINATIONS_API_KEY": "secret"},
            )
            with self.assertRaises(RuntimeError):
                provider.generate_image("prompt", "unused.png")

    def test_rejects_empty_prompt(self):
        provider = PollinationsProvider(environ={"POLLINATIONS_API_KEY": "secret"})
        with self.assertRaises(ValueError):
            provider.generate_image("  ", "unused.png")


if __name__ == "__main__":
    unittest.main()
