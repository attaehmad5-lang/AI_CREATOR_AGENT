"""Tests for bounded, value-safe sensitive-data inspection."""
import unittest

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

    def test_oversized_or_deep_payload_fails_closed(self):
        policy = SensitiveDataPolicy(max_text_chars=10, max_depth=2)
        self.assertEqual(policy.inspect({"prompt": "x" * 11})["status"], "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED")
        self.assertFalse(policy.inspect({"a": {"b": {"c": "safe"}}})["allowed"])

    def test_invalid_policy_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(blocked_categories=("everything",))
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(max_nodes=0)


if __name__ == "__main__":
    unittest.main()
