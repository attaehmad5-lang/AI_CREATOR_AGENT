"""Regression tests for safe provider billing and quota error classification."""
import unittest

from projects.provider_store.provider_error_policy import classify_provider_error


class ProviderErrorPolicyTests(unittest.TestCase):
    def test_credit_exhaustion_never_retries_even_if_provider_uses_429(self):
        result = classify_provider_error(
            429, provider_code="insufficient_quota", retry_after_seconds=30
        )
        self.assertEqual(result["status"], "CREDITS_EXHAUSTED")
        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["action"], "stop_paid_attempt_and_refresh_verified_balance"
        )
        self.assertNotIn("retry_after_seconds", result)

    def test_payment_required_stops_without_retry(self):
        result = classify_provider_error(402)
        self.assertEqual(result["status"], "BILLING_ACTION_REQUIRED")
        self.assertFalse(result["retryable"])

    def test_provider_billing_code_is_normalized(self):
        result = classify_provider_error(
            400, provider_code="billing-hard-limit-reached"
        )
        self.assertEqual(result["status"], "CREDITS_EXHAUSTED")
        self.assertFalse(result["retryable"])

    def test_auth_and_permission_failures_are_not_retried(self):
        self.assertFalse(classify_provider_error(401)["retryable"])
        self.assertEqual(
            classify_provider_error(403)["status"], "PERMISSION_DENIED"
        )

    def test_rate_limit_has_bounded_numeric_retry_after(self):
        result = classify_provider_error(429, retry_after_seconds=999999)
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after_seconds"], 300.0)

    def test_invalid_retry_after_is_ignored(self):
        for value in ("30", float("nan"), float("inf"), -1, True, 10**1000):
            with self.subTest(value=value):
                result = classify_provider_error(429, retry_after_seconds=value)
                self.assertNotIn("retry_after_seconds", result)

    def test_transient_server_errors_can_retry_with_policy(self):
        result = classify_provider_error(503)
        self.assertEqual(result["status"], "PROVIDER_TEMPORARILY_UNAVAILABLE")
        self.assertTrue(result["retryable"])
        self.assertIn("idempotency", result["action"])

    def test_malformed_inputs_fail_closed(self):
        for status in (True, 99, 600, "429", None):
            with self.subTest(status=status):
                result = classify_provider_error(status)
                self.assertEqual(result["status"], "INVALID_PROVIDER_ERROR_INPUT")
                self.assertFalse(result["retryable"])

    def test_pollinations_insufficient_balance_is_provider_specific(self):
        result = classify_provider_error(
            402, provider_id="pollinations", provider_code="INSUFFICIENT_BALANCE"
        )
        self.assertEqual(result["status"], "CREDITS_EXHAUSTED")
        self.assertEqual(result["category"], "credits_exhausted")
        self.assertFalse(result["retryable"])

    def test_pollinations_key_budget_exhaustion_is_not_misreported_as_wallet_empty(self):
        result = classify_provider_error(
            402, provider_id="pollinations", provider_code="KEY_BUDGET_EXHAUSTED"
        )
        self.assertEqual(result["status"], "PROVIDER_KEY_BUDGET_EXHAUSTED")
        self.assertEqual(result["category"], "billing")
        self.assertEqual(
            result["action"], "stop_attempt_and_review_provider_api_key_budget"
        )
        self.assertFalse(result["retryable"])

    def test_pollinations_paid_only_model_requires_paid_balance(self):
        result = classify_provider_error(
            402, provider_id="pollinations", provider_code="QUEST_POLLEN_ONLY"
        )
        self.assertEqual(result["status"], "PAID_BALANCE_REQUIRED")
        self.assertEqual(result["category"], "billing")
        self.assertFalse(result["retryable"])

    def test_pollinations_billable_output_limit_failure_never_retries_or_falls_back(self):
        result = classify_provider_error(
            400, provider_id="pollinations",
            provider_code="output_token_limit_exceeded",
        )
        self.assertEqual(result["status"], "BILLABLE_OUTPUT_LIMIT_FAILURE")
        self.assertEqual(result["category"], "billing")
        self.assertFalse(result["retryable"])
        self.assertEqual(
            result["action"], "retain_reservation_and_reconcile_usage_before_retry_or_fallback"
        )

    def test_pollinations_codes_are_not_applied_to_other_providers(self):
        credit = classify_provider_error(
            402, provider_id="huggingface", provider_code="INSUFFICIENT_BALANCE"
        )
        billed = classify_provider_error(
            400, provider_id="huggingface",
            provider_code="output_token_limit_exceeded",
        )
        self.assertEqual(credit["status"], "BILLING_ACTION_REQUIRED")
        self.assertEqual(billed["status"], "PROVIDER_REQUEST_REJECTED")

    def test_unknown_error_code_is_never_echoed(self):
        result = classify_provider_error(
            400, provider_code="secret-token-value"
        )
        self.assertEqual(result["status"], "PROVIDER_REQUEST_REJECTED")
        self.assertNotIn("provider_code", result)
        self.assertNotIn("secret-token-value", repr(result))


if __name__ == "__main__":
    unittest.main()
