import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from projects.provider_store.oauth_pkce_manager import ProviderOAuthPKCEManager


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.payload[:size] if size >= 0 else self.payload

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, *responses, error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


class ProviderOAuthPKCEManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.environ = {"PROVIDER_APP_CLIENT_ID": "public-client-id"}
        self.auth_url = "https://login.example.com/oauth/authorize"
        self.token_url = "https://login.example.com/oauth/token"
        self.redirect_uri = "https://app.example.com/oauth/callback"
        self.allowed_hosts = ["login.example.com"]
        self.response = _Response({
            "access_token": "access-secret-value",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "refresh-secret-value",
            "scope": "usage profile",
        })
        self.opener = _Opener(self.response)
        self.manager = ProviderOAuthPKCEManager(
            environ=self.environ, opener=self.opener, clock=lambda: self.now[0]
        )

    def begin(self, manager=None, **overrides):
        manager = manager or self.manager
        args = {
            "provider_id": "provider",
            "authorization_url": self.auth_url,
            "token_url": self.token_url,
            "client_id_env": "PROVIDER_APP_CLIENT_ID",
            "redirect_uri": self.redirect_uri,
            "allowed_redirect_uris": [self.redirect_uri],
            "allowed_hosts": self.allowed_hosts,
            "scopes": ["usage", "profile"],
        }
        args.update(overrides)
        return manager.begin_authorization(**args)

    def test_pollinations_helper_uses_official_pkce_endpoints_and_scopes(self):
        manager = ProviderOAuthPKCEManager(
            environ={"POLLINATIONS_APP_KEY": "pk_public_app_key"},
            opener=_Opener(), clock=lambda: self.now[0],
        )
        redirect_uri = "https://app.example.com/pollinations/callback"
        result = manager.begin_pollinations_authorization(
            redirect_uri=redirect_uri,
            allowed_redirect_uris=[redirect_uri],
        )
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        parsed = urlsplit(result["authorization_url"])
        self.assertEqual(parsed.netloc, "enter.pollinations.ai")
        self.assertEqual(parsed.path, "/authorize")
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["pk_public_app_key"])
        self.assertEqual(query["scope"], ["profile usage"])
        self.assertEqual(
            manager._pending[result["state"]]["token_url"],
            "https://enter.pollinations.ai/api/oauth/token",
        )

    def test_authorization_url_uses_pkce_s256_and_random_state(self):
        result = self.begin()
        self.assertEqual(result["status"], "AUTHORIZATION_REQUIRED")
        query = parse_qs(urlsplit(result["authorization_url"]).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["public-client-id"])
        self.assertEqual(query["scope"], ["usage profile"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], [result["state"]])
        self.assertEqual(len(query["code_challenge"][0]), 43)
        self.assertEqual(result["pkce_method"], "S256")
        self.assertNotIn("code_verifier", result)
        self.assertNotIn("access-secret-value", repr(result))

    def test_code_exchange_uses_verifier_and_keeps_tokens_out_of_status(self):
        begun = self.begin()
        result = self.manager.exchange_code("provider", state=begun["state"], code="one-time-code")
        self.assertEqual(result["status"], "OAUTH_CONNECTED")
        self.assertTrue(result["refresh_available"])
        self.assertEqual(result["scopes"], ["usage", "profile"])
        self.assertEqual(self.manager.get_access_token("provider"), "access-secret-value")
        visible = self.manager.status("provider")
        self.assertEqual(visible["status"], "CONNECTED")
        self.assertNotIn("access-secret-value", repr(visible))
        self.assertNotIn("refresh-secret-value", repr(visible))
        request, timeout = self.opener.calls[0]
        form = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["one-time-code"])
        self.assertEqual(form["client_id"], ["public-client-id"])
        self.assertEqual(form["redirect_uri"], [self.redirect_uri])
        verifier = form["code_verifier"][0]
        import base64
        import hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        self.assertEqual(parse_qs(urlsplit(begun["authorization_url"]).query)["code_challenge"], [challenge])
        self.assertEqual(timeout, 10.0)
        self.assertTrue(self.response.closed)

    def test_state_is_one_time_and_bound_to_provider(self):
        begun = self.begin()
        wrong_provider = self.manager.exchange_code("other", state=begun["state"], code="code")
        self.assertEqual(wrong_provider["status"], "OAUTH_STATE_NOT_FOUND")
        self.assertIn(begun["state"], self.manager._pending)
        first = self.manager.exchange_code("provider", state=begun["state"], code="code")
        self.assertEqual(first["status"], "OAUTH_CONNECTED")
        second = self.manager.exchange_code("provider", state=begun["state"], code="code")
        self.assertEqual(second["status"], "OAUTH_STATE_NOT_FOUND")

    def test_concurrent_code_exchange_consumes_state_once(self):
        opener = _Opener(_Response({
            "access_token": "single-use-access", "token_type": "Bearer",
            "expires_in": 3600,
        }))
        manager = ProviderOAuthPKCEManager(
            environ=self.environ, opener=opener, clock=lambda: self.now[0]
        )
        begun = self.begin(manager)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: manager.exchange_code(
                    "provider", state=begun["state"], code="single-use-code"
                ),
                range(2),
            ))
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["OAUTH_CONNECTED", "OAUTH_STATE_NOT_FOUND"],
        )
        self.assertEqual(len(opener.calls), 1)

    def test_expired_authorization_state_is_rejected(self):
        manager = ProviderOAuthPKCEManager(
            environ=self.environ, opener=_Opener(), clock=lambda: self.now[0],
            state_ttl_seconds=60,
        )
        begun = self.begin(manager)
        self.now[0] = 1060.0
        result = manager.exchange_code("provider", state=begun["state"], code="code")
        self.assertEqual(result["status"], "OAUTH_STATE_EXPIRED")
        self.assertEqual(manager.opener.calls, [])

    def test_near_expiry_access_token_is_not_returned_for_new_requests(self):
        opener = _Opener(_Response({
            "access_token": "short-lived-token", "token_type": "Bearer", "expires_in": 20,
        }))
        manager = ProviderOAuthPKCEManager(
            environ=self.environ, opener=opener, clock=lambda: self.now[0]
        )
        begun = self.begin(manager)
        result = manager.exchange_code("provider", state=begun["state"], code="code")
        self.assertEqual(result["status"], "OAUTH_CONNECTED")
        self.assertIsNone(manager.get_access_token("provider"))
        self.assertEqual(manager.status("provider")["status"], "TOKEN_EXPIRING")

    def test_refresh_rotates_access_and_refresh_tokens(self):
        begun = self.begin()
        self.assertEqual(
            self.manager.exchange_code("provider", state=begun["state"], code="code")["status"],
            "OAUTH_CONNECTED",
        )
        refresh_response = _Response({
            "access_token": "new-access-token",
            "token_type": "Bearer",
            "expires_in": 7200,
            "refresh_token": "new-refresh-token",
            "scope": "usage",
        })
        self.opener.responses.append(refresh_response)
        result = self.manager.refresh("provider")
        self.assertEqual(result["status"], "OAUTH_CONNECTED")
        self.assertEqual(self.manager.get_access_token("provider"), "new-access-token")
        self.assertTrue(result["refresh_available"])
        self.assertNotIn("new-access-token", repr(result))
        form = parse_qs(self.opener.calls[-1][0].data.decode("utf-8"))
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["refresh-secret-value"])
        self.assertTrue(refresh_response.closed)

    def test_refresh_is_explicitly_unsupported_without_refresh_token(self):
        opener = _Opener(_Response({
            "access_token": "access-only", "token_type": "Bearer", "expires_in": 3600,
        }))
        manager = ProviderOAuthPKCEManager(environ=self.environ, opener=opener, clock=lambda: self.now[0])
        begun = self.begin(manager)
        self.assertEqual(manager.exchange_code("provider", state=begun["state"], code="code")["status"], "OAUTH_CONNECTED")
        self.assertEqual(manager.refresh("provider")["status"], "REFRESH_NOT_SUPPORTED")
        self.assertEqual(len(opener.calls), 1)

    def test_unsafe_endpoints_and_invalid_redirect_are_rejected_before_network(self):
        for result in (
            self.begin(authorization_url="http://login.example.com/oauth/authorize"),
            self.begin(token_url="https://evil.example.com/token"),
            self.begin(redirect_uri="https://user:pass@app.example.com/callback"),
            self.begin(redirect_uri="https://app.example.com/callback?token=secret"),
            self.begin(client_id_env="bad-name"),
        ):
            self.assertNotEqual(result["status"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(self.opener.calls, [])

    def test_missing_client_id_and_invalid_scopes_are_rejected(self):
        manager = ProviderOAuthPKCEManager(environ={}, opener=self.opener, clock=lambda: self.now[0])
        self.assertEqual(self.begin(manager)["status"], "CLIENT_ID_MISSING_OR_INVALID")
        self.assertEqual(self.begin(scopes=["valid", "bad scope"])["status"], "INVALID_SCOPES")
        self.assertEqual(self.opener.calls, [])

    def test_invalid_token_response_never_connects(self):
        for payload, expected in (
            ({"access_token": "", "token_type": "Bearer", "expires_in": 3600}, "OAUTH_ACCESS_TOKEN_INVALID"),
            ({"access_token": "bad token", "token_type": "Bearer", "expires_in": 3600}, "OAUTH_ACCESS_TOKEN_INVALID"),
            ({"access_token": "token", "token_type": "Basic", "expires_in": 3600}, "OAUTH_TOKEN_TYPE_UNSUPPORTED"),
            ({"access_token": "token", "token_type": "Bearer", "expires_in": float("nan")}, "OAUTH_EXPIRY_INVALID"),
            ({"access_token": "token", "token_type": "Bearer", "expires_in": 10**1000}, "OAUTH_EXPIRY_INVALID"),
            ({"access_token": "token", "token_type": "Bearer", "expires_in": 3600, "scope": "usage\nsecret"}, "OAUTH_SCOPE_INVALID"),
            ({"access_token": "token", "token_type": "Bearer", "expires_in": 3600, "scope": "usage admin"}, "OAUTH_SCOPE_ESCALATION"),
        ):
            opener = _Opener(_Response(payload))
            manager = ProviderOAuthPKCEManager(environ=self.environ, opener=opener, clock=lambda: self.now[0])
            begun = self.begin(manager)
            result = manager.exchange_code("provider", state=begun["state"], code="code")
            self.assertEqual(result["status"], expected)
            self.assertIsNone(manager.get_access_token("provider"))

    def test_http_errors_and_oversized_or_invalid_responses_fail_safely(self):
        error = HTTPError(self.token_url, 401, "Unauthorized", {}, None)
        opener = _Opener(error=error)
        manager = ProviderOAuthPKCEManager(environ=self.environ, opener=opener, clock=lambda: self.now[0])
        begun = self.begin(manager)
        result = manager.exchange_code("provider", state=begun["state"], code="code")
        self.assertEqual(result["status"], "OAUTH_TOKEN_HTTP_ERROR")
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn("Unauthorized", repr(result))

        for body, expected in (
            (b"x" * 300, "OAUTH_TOKEN_RESPONSE_TOO_LARGE"),
            (b"not-json", "OAUTH_TOKEN_RESPONSE_INVALID"),
        ):
            response = _Response(body)
            opener = _Opener(response)
            manager = ProviderOAuthPKCEManager(
                environ=self.environ, opener=opener, clock=lambda: self.now[0],
                max_response_bytes=256,
            )
            begun = self.begin(manager)
            result = manager.exchange_code("provider", state=begun["state"], code="code")
            self.assertEqual(result["status"], expected)
            self.assertTrue(response.closed)

    def test_clear_removes_in_memory_credentials(self):
        begun = self.begin()
        self.manager.exchange_code("provider", state=begun["state"], code="code")
        result = self.manager.clear("provider")
        self.assertEqual(result["status"], "CLEARED")
        self.assertIsNone(self.manager.get_access_token("provider"))
        self.assertEqual(self.manager.status("provider")["status"], "DISCONNECTED")


if __name__ == "__main__":
    unittest.main()
