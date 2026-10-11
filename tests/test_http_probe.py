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

    def test_provider_specific_auth_header_defaults_are_supported(self):
        cases = [
            ("elevenlabs", "ELEVENLABS_API_KEY", "xi-api-key", "secret-eleven", "secret-eleven"),
            ("fal_ai", "FAL_KEY", "authorization", "secret-fal", "Key secret-fal"),
        ]
        for provider_id, env_name, expected_header, secret, expected_value in cases:
            with self.subTest(provider=provider_id):
                opener = _Opener()
                probe = ProviderHTTPConnectivityProbe(environ={env_name: secret}, opener=opener)
                result = probe({
                    "id": provider_id, "auth_type": "API_KEY",
                    "allowed_hosts": ["api.example.com"],
                    "configuration": {"endpoint": "https://api.example.com/health"},
                })
                self.assertTrue(result["ok"])
                headers = {key.casefold(): value for key, value in opener.calls[0][0].header_items()}
                self.assertEqual(headers[expected_header], expected_value)

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

    def test_redirects_are_rejected_and_http_error_response_is_closed(self):
        error = HTTPError(
            "https://api.example.com/health", 302, "Found",
            {"Location": "https://evil.example.com/redirect"}, None,
        )
        opener = _Opener(error=error)
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaises(ConnectionError):
            probe({
                "id": "public_provider", "auth_type": "NONE",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertTrue(error.closed)

    def test_google_provider_uses_header_auth_and_known_environment_reference(self):
        secret = "google-key-private"
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(environ={"GOOGLE_API_KEY": secret}, opener=opener)
        result = probe({
            "id": "google_image", "auth_type": "API_KEY",
            "allowed_hosts": ["api.example.com"],
            "configuration": {"endpoint": "https://api.example.com/health"},
        })
        self.assertTrue(result["ok"])
        headers = {name.casefold(): value for name, value in opener.calls[0][0].header_items()}
        self.assertEqual(headers.get("x-goog-api-key"), secret)
        self.assertNotIn(secret, repr(result))

    def test_api_auth_fails_closed_for_unknown_or_malformed_environment_reference(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(environ={"HF_TOKEN": "private"}, opener=opener)
        provider = {
            "id": "custom", "auth_type": "API_KEY", "token_env": "HF_TOKEN\\nEVIL",
            "allowed_hosts": ["api.example.com"],
            "configuration": {"endpoint": "https://api.example.com/health"},
        }
        with self.assertRaises(PermissionError):
            probe(provider)
        self.assertEqual(opener.calls, [])

    def test_custom_credential_env_reference_is_supported(self):
        secret = "custom-provider-secret"
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(
            environ={"CUSTOM_CREDENTIAL": secret}, opener=opener
        )
        result = probe({
            "id": "custom", "auth_type": "API_KEY",
            "credential_env": "CUSTOM_CREDENTIAL",
            "allowed_hosts": ["api.example.com"],
            "configuration": {"endpoint": "https://api.example.com/health"},
        })
        self.assertTrue(result["ok"])
        headers = {key.casefold(): value for key, value in opener.calls[0][0].header_items()}
        self.assertEqual(headers["authorization"], f"Bearer {secret}")
        self.assertNotIn(secret, repr(result))

    def test_host_allowlist_is_required_even_for_public_https_hosts(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaises(ValueError):
            probe({
                "id": "public", "auth_type": "NONE",
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

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
        probe = ProviderHTTPConnectivityProbe(opener=_Opener())
        provider = {
            "id": "p", "auth_type": "NONE", "allowed_hosts": ["api.example.com"],
            "configuration": {"endpoint": "https://api.example.com/health"},
        }
        with self.assertRaises(ValueError):
            probe(provider, timeout_seconds=float("nan"))
        with self.assertRaises(ValueError):
            probe(provider, timeout_seconds=float("inf"))

    def test_malformed_credential_header_value_is_rejected(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(
            environ={"HF_TOKEN": "private\r\nX-Evil: injected"}, opener=opener
        )
        with self.assertRaisesRegex(PermissionError, "credential format is invalid"):
            probe({
                "id": "huggingface", "auth_type": "API_TOKEN", "token_env": "HF_TOKEN",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

    def test_secret_cannot_be_sent_in_arbitrary_http_headers(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(environ={"HF_TOKEN": "private"}, opener=opener)
        with self.assertRaisesRegex(ValueError, "header name is not permitted"):
            probe({
                "id": "huggingface", "auth_type": "API_TOKEN",
                "token_env": "HF_TOKEN", "auth_header_name": "Host",
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

    def test_null_authentication_type_is_rejected(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaisesRegex(ValueError, "INVALID_AUTH_TYPE"):
            probe({
                "id": "p", "auth_type": None,
                "allowed_hosts": ["api.example.com"],
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])

    def test_missing_host_allowlist_is_rejected_before_network(self):
        opener = _Opener()
        probe = ProviderHTTPConnectivityProbe(opener=opener)
        with self.assertRaisesRegex(ValueError, "HOST_ALLOWLIST_REQUIRED"):
            probe({
                "id": "p", "auth_type": "NONE",
                "configuration": {"endpoint": "https://api.example.com/health"},
            })
        self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
