import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from projects.provider_store.account_balance_probe import ProviderJSONBalanceProbe
from projects.provider_store.credit_manager import ProviderCreditManager


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
    def __init__(self, response=None, error=None):
        self.response = response or _Response({"totalBalance": 2.75})
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class ProviderJSONBalanceProbeTests(unittest.TestCase):
    def make_probe(self, opener=None, **overrides):
        options = {
            "endpoint": "https://api.example.com/account/balance",
            "allowed_hosts": ["api.example.com"],
            "token_env": "PROVIDER_TOKEN",
            "unit": "credits",
            "balance_field": "totalBalance",
            "source": "official-account-balance-endpoint",
            "authentication_required": True,
            "balance_field_is_available_credits": True,
            "environ": {"PROVIDER_TOKEN": "private-token"},
            "opener": opener or _Opener(),
        }
        options.update(overrides)
        return ProviderJSONBalanceProbe(**options)

    def test_valid_balance_is_normalized_without_exposing_secret_or_body(self):
        opener = _Opener(_Response({"totalBalance": 2.75, "private": "do-not-return"}))
        probe = self.make_probe(opener, unit="POLLEN")
        result = probe("pollinations")
        self.assertEqual(result, {
            "status": "BALANCE_VERIFIED",
            "provider": "pollinations",
            "verified": True,
            "free_credits_remaining": 2.75,
            "unit": "POLLEN",
            "source": "official-account-balance-endpoint",
        })
        self.assertEqual(opener.calls[0][0].get_method(), "GET")
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Bearer private-token")
        self.assertEqual(opener.calls[0][1], 5.0)
        self.assertNotIn("private-token", repr(result))
        self.assertNotIn("do-not-return", repr(result))
        self.assertTrue(opener.response.closed)

    def test_verified_balance_probe_integrates_with_credit_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = ProviderCreditManager(Path(temporary) / "credits.json")
            probe = self.make_probe(_Opener(_Response({"totalBalance": 3.5})), unit="POLLEN")
            result = manager.refresh("pollinations", probe)
            self.assertEqual(result["status"], "CREDIT_BALANCE_UPDATED")
            self.assertEqual(manager.usable_free_credits(unit="POLLEN"), {"pollinations": 3.5})

    def test_missing_credentials_fail_before_network(self):
        opener = _Opener()
        probe = self.make_probe(opener, environ={})
        result = probe("provider")
        self.assertEqual(result["status"], "CREDENTIAL_MISSING_OR_INVALID")
        self.assertFalse(result["verified"])
        self.assertEqual(opener.calls, [])

    def test_invalid_balance_shapes_fail_closed(self):
        cases = [
            ({}, "BALANCE_FIELD_MISSING"),
            ({"totalBalance": True}, "BALANCE_AMOUNT_INVALID"),
            ({"totalBalance": -1}, "BALANCE_AMOUNT_INVALID"),
            ({"totalBalance": float("nan")}, "BALANCE_RESPONSE_INVALID"),
            ({"totalBalance": "5.0"}, "BALANCE_AMOUNT_INVALID"),
            ({"totalBalance": 10**1000}, "BALANCE_AMOUNT_INVALID"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=repr(payload)):
                result = self.make_probe(_Opener(_Response(payload)))("provider")
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["verified"])

    def test_duplicate_json_keys_and_nonstandard_constants_are_rejected(self):
        for body in (
            b'{"totalBalance": 2, "totalBalance": 9}',
            b'{"totalBalance": NaN}',
            b'{"totalBalance": Infinity}',
        ):
            with self.subTest(body=body):
                result = self.make_probe(_Opener(_Response(body)))("provider")
                self.assertEqual(result["status"], "BALANCE_RESPONSE_INVALID")
                self.assertFalse(result["verified"])

    def test_unicode_control_and_line_separator_credentials_fail_before_network(self):
        for secret in ("token" + chr(0x85) + "injected", "token" + chr(0x2028) + "injected", "token" + chr(0x2029) + "injected"):
            with self.subTest(secret=repr(secret)):
                opener = _Opener()
                result = self.make_probe(opener, environ={"PROVIDER_TOKEN": secret})("provider")
                self.assertEqual(result["status"], "CREDENTIAL_MISSING_OR_INVALID")
                self.assertEqual(opener.calls, [])

    def test_untrusted_endpoint_and_ambiguous_balance_semantics_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make_probe(endpoint="https://evil.example.com/account/balance")
        with self.assertRaisesRegex(ValueError, "HOST_ALLOWLIST_REQUIRED"):
            self.make_probe(allowed_hosts=[])
        with self.assertRaisesRegex(ValueError, "DOCUMENTED_AUTHENTICATION_REQUIRED"):
            self.make_probe(authentication_required=False)
        with self.assertRaisesRegex(ValueError, "BALANCE_FIELD_SEMANTICS_REQUIRED"):
            self.make_probe(balance_field_is_available_credits=False)
        with self.assertRaisesRegex(ValueError, "INVALID_AUTH_HEADER"):
            self.make_probe(auth_header_name="Host")
        with self.assertRaisesRegex(ValueError, "INVALID_TIMEOUT"):
            self.make_probe(timeout_seconds=10**1000)

    def test_oversized_and_invalid_json_responses_are_rejected_and_closed(self):
        for payload, expected in [
            (b"x" * 100, "BALANCE_RESPONSE_TOO_LARGE"),
            (b"{invalid", "BALANCE_RESPONSE_INVALID"),
        ]:
            with self.subTest(expected=expected):
                opener = _Opener(_Response(payload))
                probe = self.make_probe(opener, max_response_bytes=16)
                result = probe("provider")
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["verified"])
                self.assertTrue(opener.response.closed)

    def test_http_auth_failure_never_returns_provider_body_or_token(self):
        error = HTTPError(
            "https://api.example.com/account/balance", 403,
            "Forbidden private-token", {}, None,
        )
        opener = _Opener(error=error)
        result = self.make_probe(opener)("provider")
        self.assertEqual(result["status"], "BALANCE_AUTH_FAILED")
        self.assertEqual(result["http_status"], 403)
        self.assertNotIn("private-token", repr(result))

    def test_rate_limit_preserves_bounded_retry_after_without_leaking_body(self):
        error = HTTPError(
            "https://api.example.com/account/balance", 429,
            "Too Many Requests", {"Retry-After": "3"}, None,
        )
        result = self.make_probe(_Opener(error=error))("provider")
        self.assertEqual(result["status"], "BALANCE_HTTP_ERROR")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after"], "3")
        self.assertFalse(result["verified"])

    def test_malformed_retry_after_is_not_propagated(self):
        error = HTTPError(
            "https://api.example.com/account/balance", 429,
            "Too Many Requests", {"Retry-After": "bad\nsecret"}, None,
        )
        result = self.make_probe(_Opener(error=error))("provider")
        self.assertNotIn("retry_after", result)
        self.assertNotIn("secret", repr(result))

    def test_redirects_and_invalid_provider_ids_are_not_followed(self):
        opener = _Opener(error=HTTPError(
            "https://api.example.com/account/balance", 302, "redirect", {}, None
        ))
        result = self.make_probe(opener)("provider")
        self.assertFalse(result["verified"])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.make_probe()("../bad")["status"], "INVALID_PROVIDER")


if __name__ == "__main__":
    unittest.main()
