import unittest

from projects.provider_store.oauth2_refresh_manager import OAuth2RefreshTokenManager


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
            return {"access_token": "access-2", "token_type": "Bearer", "expires_in": 600}

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
