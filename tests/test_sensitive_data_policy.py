"""Tests for bounded, value-safe sensitive-data inspection."""
import unittest
from collections import UserDict

from projects.provider_store.sensitive_data_policy import SensitiveDataPolicy


class SensitiveDataPolicyTests(unittest.TestCase):
    def test_credentials_are_blocked_without_echoing_values(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        result = SensitiveDataPolicy().inspect({
            "prompt": "create a cinematic image",
            "headers": {"Authorization": f"Bearer {secret}"},
        })
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertIn("credential", result["blocked_categories"])
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("Bearer", repr(result))

    def test_email_and_phone_are_reported_but_policy_is_configurable(self):
        payload = {"prompt": "Contact jane@example.com or call +1 (415) 555-0134"}
        default = SensitiveDataPolicy().inspect(payload)
        self.assertTrue(default["allowed"])
        self.assertEqual({x["category"] for x in default["findings"]}, {"email", "phone"})
        strict = SensitiveDataPolicy(blocked_categories=("credential", "email", "phone")).inspect(payload)
        self.assertFalse(strict["allowed"])
        self.assertEqual(strict["status"], "SENSITIVE_DATA_BLOCKED")

    def test_common_provider_tokens_and_labeled_secrets_are_detected(self):
        policy = SensitiveDataPolicy()
        for value in ("api_key=not-a-real-but-long-secret", "hf_abcdefghijklmnop1234", "AKIA1234567890ABCDEF"):
            with self.subTest(value=value):
                result = policy.inspect({"options": value})
                self.assertFalse(result["allowed"])
                self.assertNotIn(value, repr(result))

    def test_clean_payload_is_allowed(self):
        result = SensitiveDataPolicy().inspect({"prompt": "A cinematic mountain landscape at dawn"})
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_CLEAR")
        self.assertEqual(result["findings"], [])
        self.assertEqual(
            SensitiveDataPolicy().inspect({"date": "2026-10-16"})["findings"],
            [],
        )
        mixed = SensitiveDataPolicy().inspect({
            "date_and_phone": "2026-10-16 415-555-0134"
        })
        self.assertEqual({x["category"] for x in mixed["findings"]}, {"phone"})

    def test_oversized_or_deep_payload_fails_closed(self):
        policy = SensitiveDataPolicy(max_text_chars=10, max_depth=2)
        self.assertEqual(policy.inspect({"prompt": "x" * 11})["status"], "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED")
        self.assertFalse(policy.inspect({"a": {"b": {"c": "safe"}}})["allowed"])

    def test_invalid_policy_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(blocked_categories=("everything",))
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(max_nodes=0)



    def test_sensitive_field_names_are_scanned_and_reference_metadata_is_ignored(self):
        payload = {
            "providerConfig": UserDict({
                "api_key": "opaque-value-one",
                "api_token": "opaque-api-token-value",
                "clientSecret": "opaque-value-two",
                "refresh_token": "opaque-value-three",
                "token_env": "PROVIDER_TOKEN",
                "secret_ref": "vault://provider/token",
                "api_key_configured": True,
            })
        }
        result = SensitiveDataPolicy().inspect(payload)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertEqual(
            {item["path"] for item in result["findings"]},
            {"$.providerConfig.api_key", "$.providerConfig.api_token", "$.providerConfig.clientSecret", "$.providerConfig.refresh_token"},
        )
        self.assertNotIn("opaque-value", repr(result))
        self.assertNotIn("opaque-api-token-value", repr(result))
        self.assertNotIn("PROVIDER_TOKEN", repr(result))

    def test_additional_token_forms_basic_auth_private_key_and_url_credentials(self):
        jwt_value = "eyJ" + ("a" * 12) + "." + ("b" * 12) + "." + ("c" * 16)
        private_key = "-----BEGIN " + "PRIVATE KEY" + "-----\n" + "synthetic-data" + "\n-----END " + "PRIVATE KEY" + "-----"
        samples = (
            "Authorization: Basic " + ("a" * 24),
            "access_token=" + jwt_value,
            "endpoint=https://user:password@example.invalid/resource",
            private_key,
            "service " + "xoxb-" + ("1" * 8) + "-" + ("a" * 8),
            "cloud " + "AIza" + ("a" * 24),
        )
        for sample in samples:
            with self.subTest(sample_type=type(sample).__name__):
                result = SensitiveDataPolicy().inspect({"nested": [sample]})
                self.assertFalse(result["allowed"])
                self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
                self.assertNotIn(sample, repr(result))

    def test_payment_card_luhn_detection_is_configurable(self):
        digits = str(6011) + ("0" * 11)
        parity = (len(digits) + 1) % 2
        total = 0
        for index, char in enumerate(digits):
            value = int(char)
            if index % 2 == parity:
                value *= 2
                if value > 9:
                    value -= 9
            total += value
        number = digits + str((-total) % 10)
        report = SensitiveDataPolicy().inspect({"billing": number})
        self.assertTrue(report["allowed"])
        self.assertIn({"category": "payment_card", "path": "$.billing"}, report["findings"])
        blocked = SensitiveDataPolicy(
            blocked_categories=("credential", "payment_card")
        ).inspect({"billing": number})
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["blocked_categories"], ["payment_card"])
        self.assertNotIn(number, repr(blocked))

    def test_custom_mapping_errors_and_cycles_fail_closed(self):
        class BrokenMapping(UserDict):
            def items(self):
                raise RuntimeError("do not reflect internal payload")

        failed = SensitiveDataPolicy().inspect(BrokenMapping({"ordinary": "value"}))
        self.assertFalse(failed["allowed"])
        self.assertEqual(failed["status"], "SENSITIVE_DATA_SCAN_FAILED")
        self.assertNotIn("internal payload", repr(failed))

        cycle = {}
        cycle["self"] = cycle
        limited = SensitiveDataPolicy().inspect(cycle)
        self.assertFalse(limited["allowed"])
        self.assertEqual(limited["status"], "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED")
        self.assertEqual(limited["findings"], [])

if __name__ == "__main__":
    unittest.main()
