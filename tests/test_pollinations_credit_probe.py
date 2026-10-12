import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.pollinations_credit_probe import PollinationsBalanceProbe
from projects.provider_store.retry_policy import RetryPolicy


class _Response:
    def __init__(self, body=b'{"balance": 12.5}', status=200):
        self.body = body
        self.status = status
        self.closed = False
        self.read_sizes = []

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_sizes.append(size)
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


class PollinationsBalanceProbeTests(unittest.TestCase):
    def _provider(self, token="temporary-user-token", opener=None, **kwargs):
        return PollinationsBalanceProbe(lambda: token, opener=opener, **kwargs)

    def test_balance_probe_uses_only_account_endpoint_and_in_memory_bearer_token(self):
        secret = "never-persist-or-return-this"
        response = _Response(b'{"balance": 12.5}')
        opener = _Opener(response)
        probe = self._provider(secret, opener)
        result = probe("pollinations")

        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://gen.pollinations.ai/account/balance")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {secret}")
        self.assertEqual(timeout, 5.0)
        self.assertEqual(result, {
            "verified": True,
            "balance": 12.5,
            "unit": "pollen",
            "balance_type": "remaining_account_budget",
            "source": "pollinations.account.balance",
        })
        self.assertNotIn(secret, repr(result))
        self.assertTrue(response.closed)

    def test_account_balance_is_not_misclassified_as_free_credits(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProviderCreditManager(Path(directory) / "credits.json")
            opener = _Opener(_Response(b'{"balance": 7}'))
            probe = self._provider("ephemeral-token", opener)
            result = probe("pollinations")
            self.assertEqual(result["balance"], 7.0)
            self.assertEqual(result["unit"], "pollen")
            self.assertEqual(result["balance_type"], "remaining_account_budget")
            self.assertNotIn("free_credits_remaining", result)
            # ProviderCreditManager must reject this account budget as a free-credit snapshot.
            rejected = manager.refresh("pollinations", probe)
            self.assertEqual(rejected["status"], "INVALID_CREDIT_AMOUNT")
            self.assertEqual(manager.usable_free_credits(unit="pollen"), {})
            self.assertEqual(len(opener.calls), 2)
            self.assertTrue(all("/account/balance" in call[0].full_url for call in opener.calls))
            persisted = Path(directory) / "credits.json"
            if persisted.exists():
                self.assertNotIn("ephemeral-token", persisted.read_text())
            self.assertNotIn("pollinations", manager.data["providers"])

    def test_missing_or_invalid_user_authorized_token_fails_before_network(self):
        opener = _Opener()
        for token in (None, "", "bad\ntoken"):
            with self.subTest(token=repr(token)):
                probe = self._provider(token, opener)
                with self.assertRaises(PermissionError):
                    probe("pollinations")
        self.assertEqual(opener.calls, [])

    def test_unknown_provider_id_is_rejected_before_token_or_network(self):
        called = []
        probe = PollinationsBalanceProbe(lambda: called.append(True) or "token", opener=_Opener())
        with self.assertRaises(ValueError):
            probe("other-provider")
        self.assertEqual(called, [])

    def test_http_auth_and_permission_failures_do_not_leak_provider_body_or_token(self):
        opener = _Opener(error=HTTPError(
            "https://gen.pollinations.ai/account/balance", 403, "Forbidden", {}, None
        ))
        probe = self._provider("secret-token", opener)
        with self.assertRaises(PermissionError) as caught:
            probe("pollinations")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("Forbidden", str(caught.exception))

    def test_non_auth_http_errors_are_safe_and_non_retryable_details_are_not_leaked(self):
        opener = _Opener(error=HTTPError(
            "https://gen.pollinations.ai/account/balance", 429, "Too Many Requests",
            {"Retry-After": "3"}, None
        ))
        probe = self._provider("secret-token", opener)
        with self.assertRaisesRegex(ConnectionError, "HTTP 429") as caught:
            probe("pollinations")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, "3")
        decision = RetryPolicy(
            max_attempts=3, base_delay_seconds=0, max_delay_seconds=10,
            jitter_fraction=0,
        ).decide(attempt=1, exception=caught.exception)
        self.assertTrue(decision.retry)
        self.assertEqual(decision.delay_seconds, 3.0)

    def test_redirects_are_rejected(self):
        opener = _Opener(error=HTTPError(
            "https://gen.pollinations.ai/account/balance", 302, "Found", {}, None
        ))
        with self.assertRaisesRegex(ConnectionError, "redirected"):
            self._provider("secret-token", opener)("pollinations")

    def test_invalid_balance_json_and_oversized_responses_fail_closed(self):
        for body in (
            b"not-json", b"[]", b'{"balance": true}', b'{"balance": -1}',
            b'{"balance": NaN}', b'{"balance": "12"}',
        ):
            with self.subTest(body=body):
                probe = self._provider("token", _Opener(_Response(body)))
                with self.assertRaises(ValueError):
                    probe("pollinations")
        probe = self._provider(
            "token", _Opener(_Response(b'{"balance": 123456789}')),
            max_response_bytes=8,
        )
        with self.assertRaisesRegex(ValueError, "size limit"):
            probe("pollinations")

    def test_probe_limits_and_timeout_are_validated(self):
        for kwargs in (
            {"timeout_seconds": 0},
            {"timeout_seconds": float("inf")},
            {"timeout_seconds": float("nan")},
            {"timeout_seconds": 61},
            {"max_response_bytes": 1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PollinationsBalanceProbe(lambda: "token", **kwargs)

    def test_network_timeout_is_safely_reported(self):
        opener = _Opener(error=URLError(TimeoutError("private timeout detail")))
        with self.assertRaisesRegex(TimeoutError, "timed out") as caught:
            self._provider("secret-token", opener)("pollinations")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("private timeout detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
