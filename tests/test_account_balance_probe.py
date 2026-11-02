import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from projects.provider_store.account_balance_probe import (
    ProviderJSONBalanceProbe,
    PollinationsAccountBalanceProbe,
)
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
            ({"totalBalance": float("nan")}, "BALANCE_AMOUNT_INVALID"),
            ({"totalBalance": "5.0"}, "BALANCE_AMOUNT_INVALID"),
            ({"totalBalance": 10**1000}, "BALANCE_AMOUNT_INVALID"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=repr(payload)):
                result = self.make_probe(_Opener(_Response(payload)))("provider")
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["verified"])

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

    def test_http_error_cleanup_failure_preserves_classified_balance_error(self):
        class BrokenHTTPError(HTTPError):
            def close(self):
                raise RuntimeError("cleanup failure")

        error = BrokenHTTPError(
            "https://api.example.com/account/balance", 401,
            "Unauthorized", {}, None,
        )
        result = self.make_probe(_Opener(error=error))("provider")
        self.assertEqual(result["status"], "BALANCE_AUTH_FAILED")
        self.assertEqual(result["http_status"], 401)
        self.assertFalse(result["verified"])
        self.assertNotIn("cleanup failure", repr(result))

    def test_response_cleanup_failure_does_not_mask_verified_balance(self):
        class BrokenCloseResponse(_Response):
            @property
            def close(self):
                raise RuntimeError("cleanup failure")

        response = BrokenCloseResponse({"totalBalance": 4.25})
        result = self.make_probe(_Opener(response))("provider")
        self.assertEqual(result["status"], "BALANCE_VERIFIED")
        self.assertEqual(result["free_credits_remaining"], 4.25)
        self.assertTrue(result["verified"])

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



class PollinationsAccountBalanceProbeTests(unittest.TestCase):
    def make_probe(self, opener=None, **overrides):
        options = {
            "environ": {"POLLINATIONS_API_KEY": "secret-key"},
            "opener": opener or _Opener(_Response({
                "accountBalance": {"total": 4.5, "tier": 1.0, "paid": 3.5},
                "balance": 99.0,
            })),
        }
        options.update(overrides)
        return PollinationsAccountBalanceProbe(**options)

    def test_reads_account_total_and_paid_tier_without_exposing_key(self):
        opener = _Opener(_Response({
            "accountBalance": {"total": 4.5, "tier": 1.0, "paid": 3.5},
            "balance": 99.0,
        }))
        probe = self.make_probe(opener)
        result = probe()
        self.assertEqual(result["status"], "POLLINATIONS_ACCOUNT_BALANCE_VERIFIED")
        self.assertEqual(result["account_balance_total"], 4.5)
        self.assertEqual(result["tier"], 1.0)
        self.assertEqual(result["paid"], 3.5)
        self.assertEqual(result["unit"], "POLLEN")
        self.assertEqual(result["required_scope"], "account:usage")
        request = opener.calls[0][0]
        self.assertEqual(request.full_url, "https://gen.pollinations.ai/account/balance")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertTrue(opener.response.closed)
        self.assertNotIn("secret-key", repr(result))

    def test_never_treats_api_key_budget_as_account_wallet_balance(self):
        opener = _Opener(_Response({"balance": 99.0}))
        result = self.make_probe(opener)()
        self.assertEqual(result["status"], "ACCOUNT_USAGE_SCOPE_REQUIRED")
        self.assertFalse(result["verified"])
        self.assertEqual(result["required_scope"], "account:usage")
        self.assertNotIn("account_balance_total", result)
        self.assertTrue(opener.response.closed)

    def test_missing_key_fails_before_network_call(self):
        opener = _Opener()
        result = self.make_probe(opener, environ={})()
        self.assertEqual(result["status"], "CREDENTIAL_MISSING_OR_INVALID")
        self.assertEqual(opener.calls, [])

    def test_forbidden_scope_is_reported_without_provider_body(self):
        opener = _Opener(error=HTTPError(
            "https://gen.pollinations.ai/account/balance", 403,
            "Forbidden private body", {}, None,
        ))
        result = self.make_probe(opener)()
        self.assertEqual(result["status"], "ACCOUNT_USAGE_SCOPE_REQUIRED")
        self.assertEqual(result["http_status"], 403)
        self.assertNotIn("private body", repr(result))
        self.assertNotIn("secret-key", repr(result))

    def test_invalid_nested_balance_fails_closed(self):
        for value in (True, -1, "4.5", float("inf"), 10**1000):
            with self.subTest(value=repr(value)):
                result = self.make_probe(_Opener(_Response({
                    "accountBalance": {"total": value}
                })))()
                self.assertEqual(result["status"], "BALANCE_AMOUNT_INVALID")
                self.assertFalse(result["verified"])

    def test_invalid_provider_is_rejected(self):
        result = self.make_probe()("huggingface")
        self.assertEqual(result["status"], "INVALID_PROVIDER")
        self.assertFalse(result["verified"])
