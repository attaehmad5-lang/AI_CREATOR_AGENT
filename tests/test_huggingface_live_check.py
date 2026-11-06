"""Live Hugging Face credential check must use the pinned HTTPS transport."""
import unittest
from unittest.mock import patch

from scripts.huggingface_live_check import verify_token


class Response:
    def __init__(self, status=200, body=b'{"name":"safe"}'):
        self.status = status
        self.body = body
        self.headers = {}
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        return self.body[:amount] if amount >= 0 else self.body

    def close(self):
        self.closed = True


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


class HuggingFaceLiveCheckTests(unittest.TestCase):
    def test_token_check_uses_fixed_host_pinned_transport_and_closes_response(self):
        response = Response()
        opener = Opener(response)
        with patch(
            "scripts.huggingface_live_check.ProviderPinnedHTTPSOpener",
            return_value=opener,
        ):
            verify_token("hf-private-test-token")
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://huggingface.co/api/whoami-v2")
        self.assertEqual(request.get_header("Authorization"), "Bearer hf-private-test-token")
        self.assertEqual(timeout, 15)
        self.assertTrue(response.closed)

    def test_token_check_rejects_redirect_and_closes_response(self):
        response = Response(status=302)
        opener = Opener(response)
        with patch(
            "scripts.huggingface_live_check.ProviderPinnedHTTPSOpener",
            return_value=opener,
        ):
            with self.assertRaisesRegex(RuntimeError, "redirect was rejected"):
                verify_token("hf-private-test-token")
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
