"""Regression tests for the Hugging Face DNS-pinned HTTP transport."""
import unittest

from projects.provider_store import huggingface_pinned_transport as pinned


class FakeHTTPResponse:
    def __init__(self, status=200, body=b"image-data", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        return self.body[:amount] if amount >= 0 else self.body

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None):
        self.response = response or FakeHTTPResponse()
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.response


class FakeHTTPXResponse:
    def __init__(self, *, status_code, headers, content, request):
        self.status_code = status_code
        self.headers = headers
        self.content = content
        self.request = request


class FakeBaseTransport:
    pass


class FakeHTTPX:
    BaseTransport = FakeBaseTransport
    Response = FakeHTTPXResponse

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs


class RequestStub:
    def __init__(self, url, *, method="POST", headers=None, body=b"prompt"):
        self.url = url
        self.method = method
        self.headers = headers or {"authorization": "Bearer private-token", "content-type": "application/json"}
        self.stream = (body,) if body else ()
        self.extensions = {"timeout": {"connect": 3.0, "read": 7.0}}


class HuggingFacePinnedTransportTests(unittest.TestCase):
    def transport(self, opener, **kwargs):
        transport_type = pinned.build_pinned_httpx_transport(
            FakeHTTPX, opener_factory=lambda: opener, **kwargs
        )
        return transport_type()

    def test_allowed_inference_request_uses_pinned_opener_and_returns_response(self):
        opener = FakeOpener(FakeHTTPResponse(
            status=200, body=b"valid-image",
            headers={"Content-Type": "image/png", "Content-Length": "11"},
        ))
        transport = self.transport(opener)
        request = RequestStub(
            "https://router.huggingface.co/v1/images/generations?provider=auto"
        )
        response = transport.handle_request(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"valid-image")
        sent_request, timeout = opener.calls[0]
        self.assertEqual(sent_request.full_url, request.url)
        self.assertEqual(sent_request.get_method(), "POST")
        sent_headers = {k.casefold(): v for k, v in sent_request.header_items()}
        self.assertEqual(sent_headers["authorization"], "Bearer private-token")
        self.assertEqual(sent_headers["accept-encoding"], "identity")
        self.assertNotIn("host", sent_headers)
        self.assertEqual(timeout, 3.0)
        self.assertEqual(opener.response.closed, True)

    def test_unlisted_hosts_http_and_embedded_credentials_fail_before_network(self):
        for url in (
            "https://evil.example/collect",
            "https://router.huggingface.co.evil.example/path",
            "http://router.huggingface.co/path",
            "https://user:pass@router.huggingface.co/path",
            "https://router.huggingface.co:8443/path",
        ):
            with self.subTest(url=url):
                opener = FakeOpener()
                transport = self.transport(opener)
                with self.assertRaises(ValueError):
                    transport.handle_request(RequestStub(url))
                self.assertEqual(opener.calls, [])

    def test_sensitive_query_parameter_is_rejected_before_network(self):
        opener = FakeOpener()
        transport = self.transport(opener)
        with self.assertRaisesRegex(ValueError, "HUGGINGFACE_CREDENTIAL_IN_QUERY_REJECTED"):
            transport.handle_request(RequestStub(
                "https://router.huggingface.co/path?access_token=private"
            ))
        self.assertEqual(opener.calls, [])

    def test_real_huggingface_httpx_transport_contract_is_supported(self):
        try:
            from huggingface_hub.utils import httpx as hub_httpx
        except ImportError:
            self.skipTest("huggingface_hub is not installed in this test environment")
        transport_type = pinned.build_pinned_httpx_transport(
            hub_httpx, opener_factory=FakeOpener,
        )
        transport = transport_type()
        client = hub_httpx.Client(
            transport=transport, follow_redirects=False, timeout=3.0,
        )
        try:
            self.assertIsInstance(client, hub_httpx.Client)
            self.assertIsInstance(transport, hub_httpx.BaseTransport)
        finally:
            client.close()

    def test_redirect_is_rejected_and_response_is_closed(self):
        response = FakeHTTPResponse(
            status=302, headers={"Location": "https://evil.example/collect"}
        )
        opener = FakeOpener(response)
        transport = self.transport(opener)
        with self.assertRaisesRegex(ValueError, "HUGGINGFACE_REDIRECT_REJECTED"):
            transport.handle_request(RequestStub("https://router.huggingface.co/path"))
        self.assertTrue(response.closed)

    def test_oversized_request_body_is_rejected_before_network(self):
        opener = FakeOpener()
        transport = self.transport(opener, max_request_bytes=1024)
        request = RequestStub(
            "https://router.huggingface.co/path", body=b"x" * 1025
        )
        with self.assertRaisesRegex(ValueError, "HUGGINGFACE_REQUEST_BODY_TOO_LARGE"):
            transport.handle_request(request)
        self.assertEqual(opener.calls, [])

    def test_oversized_response_is_rejected_and_closed(self):
        response = FakeHTTPResponse(body=b"x" * 2049)
        opener = FakeOpener(response)
        transport = self.transport(opener, max_response_bytes=2048)
        with self.assertRaisesRegex(ValueError, "HUGGINGFACE_RESPONSE_TOO_LARGE"):
            transport.handle_request(RequestStub("https://router.huggingface.co/path"))
        self.assertTrue(response.closed)

    def test_factory_installs_redirect_disabled_http_client(self):
        original = pinned._INSTALLED
        pinned._INSTALLED = False
        captured = {}
        try:
            pinned.install_huggingface_pinned_transport(
                httpx_module=FakeHTTPX,
                set_client_factory=lambda factory: captured.setdefault("factory", factory),
                timeout_seconds=120,
            )
            client = captured["factory"]()
            self.assertFalse(client.kwargs["follow_redirects"])
            self.assertEqual(client.kwargs["timeout"], 120.0)
            self.assertIsInstance(client.kwargs["transport"], FakeBaseTransport)
        finally:
            pinned._INSTALLED = original


if __name__ == "__main__":
    unittest.main()
