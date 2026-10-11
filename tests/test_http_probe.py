import unittest
from urllib.error import HTTPError

from projects.provider_store.http_probe import ProviderHTTPConnectivityProbe


class _Response:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.status = status
        self.body = body
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body

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


class ProviderHTTPConnectivityProbeTests(unittest.TestCase):
    def test_public_https_probe_uses_timeout_and_bounds_response_read(self):
        opener = _Opener(_Response(body=b"x" * 5000))
        probe = ProviderHTTPConnectivityProbe(opener=opener, max_response_bytes=16)
        result = probe(
            {
                "id": "public_provider",
                "auth_type": "NONE",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            },
            timeout_seconds=2.5,
        )
        self.assertEqual(result, {"ok": True, "http_status": 200})
        self.assertEqual(opener.calls[0][1], 2.5)
        self.assertEqual(opener.calls[0][0].get_method(), "GET")
        self.assertEqual(len(opener.response.body), 5000)
        self.assertTrue(opener.response.closed)

    def test_token_is_read_from_environment_and_never_returned(self):
        secret = "do-not-return-this-token"
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(
            environ={"HF_TOKEN": secret}, opener=opener
        )
        result = probe({
            "id": "huggingface",
            "auth_type": "API_TOKEN",
            "allowed_hosts": ["api.example.com"],
            "configuration": {"endpoint": "https://api.example.com/health"},
        })
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), f"Bearer {secret}")
        self.assertNotIn(secret, repr(result))

    def test_missing_endpoint_and_unallowlisted_host_fail_before_network(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaises(ValueError):
            probe({"id": "p", "auth_type": "NONE", "configuration": {}})
        with self.assertRaises(ValueError):
            probe({
                "id": "p", "auth_type": "NONE",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://evil.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

    def test_authentication_failure_is_generic_and_does_not_leak_response(self):
        secret_body = b"private credential details"
        error = HTTPError(
            "https://api.example.com/health", 401, "Unauthorized", {}, None
        )
        opener = _Opener(error=error)
        probe = ProviderHTTPConnectivityProbe(
            environ={"HF_TOKEN": "secret"}, opener=opener
        )
        with self.assertRaises(PermissionError) as caught:
            probe({
                "id": "huggingface", "auth_type": "API_TOKEN",
                "token_env": "HF_TOKEN", "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertNotIn(secret_body.decode(), str(caught.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_manual_account_auth_is_not_faked_by_http_probe(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaises(PermissionError):
            probe({
                "id": "music", "auth_type": "ACCOUNT",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

    def test_invalid_probe_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderHTTPConnectivityProbe(max_response_bytes=-1)
        with self.assertRaises(ValueError):
            ProviderHTTPConnectivityProbe(environ=[])
        with self.assertRaises(ValueError):
            ProviderHTTPConnectivityProbe(opener=object())


if __name__ == "__main__":
    unittest.main()
