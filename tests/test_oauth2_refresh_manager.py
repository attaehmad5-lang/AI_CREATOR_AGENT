import unittest

from projects.provider_store.advanced.advps.transport import SecureHTTPTransport
from projects.provider_store.advanced.advps.errors import SecurityError
from projects.provider_store.oauth2_refresh_manager import OAuth2RefreshTokenManager, SecureOAuthTokenRequester


def config(**overrides):
    value = {
        "token_endpoint": "https://auth.example.com/oauth/token",
        "allowed_hosts": ["auth.example.com"],
        "client_id_env": "TEST_CLIENT_ID",
        "client_secret_env": "TEST_CLIENT_SECRET",
        "refresh_token_env": "TEST_REFRESH_TOKEN",
        "client_auth_method": "body",
        "scope": "media.generate",
    }
    value.update(overrides)
    return value


def environment():
    return {
        "TEST_CLIENT_ID": "client-id",
        "TEST_CLIENT_SECRET": "client-secret",
        "TEST_REFRESH_TOKEN": "refresh-token-old",
    }


class SecureOAuthTokenRequesterTests(unittest.TestCase):
    def test_form_exchange_uses_secure_transport_and_basic_auth(self):
        class Transport:
            timeout = 5.0

            def __init__(self):
                self.calls = []

            def request(self, method, url, *, headers, body):
                self.calls.append((method, url, headers, body))
                return {"status": 200, "body": b'{"access_token":"safe","token_type":"Bearer","expires_in":300}'}

        transport = Transport()
        requester = SecureOAuthTokenRequester(transport)
        result = requester(
            "https://auth.example.com/token",
            form_data={"grant_type": "refresh_token", "refresh_token": "rotate-me"},
            client_auth=("client", "secret"),
            timeout_seconds=10.0,
        )
        self.assertEqual(result["access_token"], "safe")
        method, url, headers, body = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://auth.example.com/token")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(headers["Authorization"], "Basic Y2xpZW50OnNlY3JldA==")
        self.assertIn(b"refresh_token=rotate-me", body)

    def test_secure_requester_rejects_non_https_status_bad_json_and_oversize(self):
        class Transport:
            timeout = 5.0

            def __init__(self, response):
                self.response = response

            def request(self, *args, **kwargs):
                return self.response

        with self.assertRaisesRegex(RuntimeError, "OAUTH_ENDPOINT_INVALID"):
            SecureOAuthTokenRequester(Transport({"status": 200, "body": b"{}"}))(
                "http://auth.example.com/token", form_data={}, client_auth=None, timeout_seconds=10
            )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_HTTP_STATUS_REJECTED"):
            SecureOAuthTokenRequester(Transport({"status": 401, "body": b"secret"}))(
                "https://auth.example.com/token", form_data={}, client_auth=None, timeout_seconds=10
            )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_RESPONSE_JSON_INVALID"):
            SecureOAuthTokenRequester(Transport({"status": 200, "body": b"not-json"}))(
                "https://auth.example.com/token", form_data={}, client_auth=None, timeout_seconds=10
            )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_RESPONSE_SIZE_INVALID"):
            SecureOAuthTokenRequester(Transport({"status": 200, "body": b"12345"}), max_response_bytes=4)(
                "https://auth.example.com/token", form_data={}, client_auth=None, timeout_seconds=10
            )

    def test_secure_requester_enforces_transport_timeout_ceiling(self):
        class Transport:
            timeout = 30.0

            def request(self, *args, **kwargs):
                raise AssertionError("transport must not be called")

        with self.assertRaisesRegex(RuntimeError, "OAUTH_TRANSPORT_TIMEOUT_POLICY"):
            SecureOAuthTokenRequester(Transport())(
                "https://auth.example.com/token", form_data={}, client_auth=None, timeout_seconds=10
            )


    def test_requester_integrates_with_allowlisted_secure_transport(self):
        class Response:
            status = 200
            def read(self, limit):
                return b'{"access_token":"integrated-token","token_type":"Bearer","expires_in":300}'
            def getheaders(self):
                return [("Content-Type", "application/json")]

        class Connection:
            def __init__(self, *args):
                self.response = Response()
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return self.response
            def close(self):
                pass

        def resolver(host, port, type=None):
            return [(2, 1, 6, "", ("93.184.216.34", port))]

        transport = SecureHTTPTransport(
            allowlist=["auth.example.com"],
            resolver=resolver,
            connection_factory=lambda *args: Connection(),
            timeout=2.0,
        )
        requester = SecureOAuthTokenRequester(transport)
        result = requester(
            "https://auth.example.com/oauth/token",
            form_data={"grant_type": "refresh_token", "refresh_token": "sensitive-refresh"},
            client_auth=None,
            timeout_seconds=2.0,
        )
        self.assertEqual(result["access_token"], "integrated-token")

        with self.assertRaises(SecurityError):
            requester(
                "https://attacker.example.net/oauth/token",
                form_data={"grant_type": "refresh_token", "refresh_token": "sensitive-refresh"},
                client_auth=None,
                timeout_seconds=2.0,
            )

class OAuth2RefreshTokenManagerTests(unittest.TestCase):
    def test_refresh_uses_injected_transport_and_caches_token_in_memory(self):
        now = [1000.0]
        calls = []

        def request_token(endpoint, *, form_data, client_auth, timeout_seconds):
            calls.append((endpoint, dict(form_data), client_auth, timeout_seconds))
            return {"access_token": "access-token-1", "token_type": "Bearer", "expires_in": 3600}

        manager = OAuth2RefreshTokenManager(
            {"provider-a": config()}, request_token=request_token,
            environ=environment(), clock=lambda: now[0], expiry_skew_seconds=10,
        )
        self.assertEqual(manager.get_access_token("provider-a"), "access-token-1")
        self.assertEqual(manager.get_access_token("provider-a"), "access-token-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "https://auth.example.com/oauth/token")
        self.assertEqual(calls[0][1]["grant_type"], "refresh_token")
        self.assertEqual(calls[0][1]["refresh_token"], "refresh-token-old")
        self.assertEqual(calls[0][1]["client_secret"], "client-secret")
        self.assertIsNone(calls[0][2])
        self.assertEqual(calls[0][3], 10.0)

    def test_expired_token_refreshes_and_rotated_refresh_token_uses_secret_sink(self):
        now = [100.0]
        refresh_values = []
        calls = []

        def request_token(endpoint, *, form_data, client_auth, timeout_seconds):
            calls.append(dict(form_data))
            if len(calls) == 1:
                return {
                    "access_token": "access-1", "token_type": "Bearer",
                    "expires_in": 120, "refresh_token": "refresh-token-new",
                }
            return {"access_token": "access-2", "token_type": "Bearer", "expires_in": 600, "refresh_token": "refresh-token-new-2"}

        manager = OAuth2RefreshTokenManager(
            {"provider-a": config(refresh_token_rotation_required=True)},
            request_token=request_token,
            environ=environment(),
            refresh_token_sink=lambda provider_id, token: refresh_values.append((provider_id, token)) or True,
            clock=lambda: now[0], expiry_skew_seconds=10,
        )
        self.assertEqual(manager.get_access_token("provider-a"), "access-1")
        self.assertEqual(refresh_values, [("provider-a", "refresh-token-new")])
        now[0] = 220.0
        self.assertEqual(manager.get_access_token("provider-a"), "access-2")
        self.assertEqual(calls[1]["refresh_token"], "refresh-token-new")

    def test_rotation_required_without_confirmed_secret_sink_fails_closed(self):
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config(refresh_token_rotation_required=True)},
            request_token=lambda *args, **kwargs: {
                "access_token": "access-1", "token_type": "Bearer",
                "expires_in": 600, "refresh_token": "refresh-token-new",
            },
            environ=environment(),
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_REFRESH_TOKEN_SINK_REQUIRED"):
            manager.get_access_token("provider-a")

        manager_with_unconfirmed_sink = OAuth2RefreshTokenManager(
            {"provider-a": config(refresh_token_rotation_required=True)},
            request_token=lambda *args, **kwargs: {
                "access_token": "access-1", "token_type": "Bearer",
                "expires_in": 600, "refresh_token": "refresh-token-new",
            },
            environ=environment(),
            refresh_token_sink=lambda provider_id, token: None,
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_REFRESH_TOKEN_PERSISTENCE_FAILED"):
            manager_with_unconfirmed_sink.get_access_token("provider-a")

    def test_required_rotation_rejects_response_without_new_refresh_token(self):
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config(refresh_token_rotation_required=True)},
            request_token=lambda *args, **kwargs: {
                "access_token": "access-token", "token_type": "Bearer", "expires_in": 300,
            },
            environ=environment(),
            refresh_token_sink=lambda provider_id, token: True,
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_REFRESH_TOKEN_ROTATION_REQUIRED"):
            manager.get_access_token("provider-a")

    def test_invalid_clock_result_fails_closed(self):
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config()},
            request_token=lambda *args, **kwargs: self.fail("token request must not run"),
            environ=environment(), clock=lambda: float("nan"),
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_CLOCK_INVALID"):
            manager.get_access_token("provider-a")

    def test_basic_client_auth_and_token_invalidation(self):
        calls = []
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config(client_auth_method="basic", scope="")},
            request_token=lambda endpoint, **kwargs: calls.append(kwargs) or {
                "access_token": "access-token", "token_type": "bearer", "expires_in": 300,
            },
            environ=environment(),
        )
        self.assertEqual(manager.get_access_token("provider-a"), "access-token")
        self.assertEqual(calls[0]["client_auth"], ("client-id", "client-secret"))
        self.assertNotIn("client_secret", calls[0]["form_data"])
        self.assertTrue(manager.invalidate("provider-a"))
        self.assertFalse(manager.invalidate("missing"))
        self.assertEqual(manager.get_access_token("provider-a"), "access-token")
        self.assertEqual(len(calls), 2)

    def test_invalid_endpoint_or_token_response_is_rejected_without_echoing_secrets(self):
        with self.assertRaisesRegex(ValueError, "allowed host"):
            OAuth2RefreshTokenManager(
                {"provider-a": config(token_endpoint="https://evil.example/oauth/token")},
                request_token=lambda *args, **kwargs: {},
                environ=environment(),
            )
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config()},
            request_token=lambda *args, **kwargs: {
                "access_token": "access-token", "token_type": "Basic", "expires_in": 300,
                "debug": "client-secret",
            },
            environ=environment(),
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_TOKEN_RESPONSE_INVALID") as caught:
            manager.get_access_token("provider-a")
        self.assertNotIn("client-secret", str(caught.exception))

    def test_provider_request_failure_is_sanitized_and_unknown_provider_rejected(self):
        manager = OAuth2RefreshTokenManager(
            {"provider-a": config()},
            request_token=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("client-secret leaked")),
            environ=environment(),
        )
        with self.assertRaisesRegex(RuntimeError, "OAUTH_TOKEN_REQUEST_FAILED") as caught:
            manager.get_access_token("provider-a")
        self.assertNotIn("client-secret", str(caught.exception))
        with self.assertRaisesRegex(RuntimeError, "OAUTH_PROVIDER_NOT_CONFIGURED"):
            manager.get_access_token("unknown-provider")


if __name__ == "__main__":
    unittest.main()
