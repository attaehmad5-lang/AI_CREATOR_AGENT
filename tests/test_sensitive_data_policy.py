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
        strict = SensitiveDataPolicy(
            blocked_categories=("credential", "email", "phone")
        ).inspect(payload)
        self.assertFalse(strict["allowed"])
        self.assertEqual(strict["status"], "SENSITIVE_DATA_BLOCKED")

    def test_sensitive_field_labels_are_detected_without_echoing_values(self):
        secret_values = [
            "Jane Example", "555 Example Street", "diagnosis details",
            "123456789", "fingerprint-template-bytes", "192.0.2.10",
            "1990-01-01",
        ]
        payload = {
            "contactEmail": secret_values[0] + " jane@example.com",
            "homeAddress": secret_values[1],
            "medicalRecord": secret_values[2],
            "national_id": secret_values[3],
            "biometric": {"fingerprintTemplate": secret_values[4]},
            "clientIp": secret_values[5],
            "dateOfBirth": secret_values[6],
        }
        result = SensitiveDataPolicy(
            blocked_categories=(
                "email", "location", "health", "government_id",
                "biometric", "network_identifier", "personal_identifier",
            )
        ).inspect(payload)
        self.assertFalse(result["allowed"])
        categories = {item["category"] for item in result["findings"]}
        self.assertTrue({
            "email", "location", "health", "government_id",
            "biometric", "network_identifier", "personal_identifier",
        }.issubset(categories))
        rendered = repr(result)
        for secret in secret_values:
            self.assertNotIn(secret, rendered)
        self.assertNotIn("jane@example.com", rendered)

    def test_payment_cards_use_luhn_validation_and_never_echo_digits(self):
        policy = SensitiveDataPolicy(blocked_categories=("credential", "financial"))
        valid_card = "4111 1111 1111 1111"
        result = policy.inspect({"prompt": f"charge card {valid_card}"})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn("4111", repr(result))
        invalid = SensitiveDataPolicy().inspect({"text": "reference 4111 1111 1111 1112"})
        self.assertNotIn("financial", {x["category"] for x in invalid["findings"]})

    def test_valid_iban_and_ssn_like_ids_are_classified(self):
        payload = {
            "bank_reference": "GB82 WEST 1234 5698 7654 32",
            "note": "SSN 123-45-6789",
        }
        result = SensitiveDataPolicy().inspect(payload)
        self.assertIn("financial", {x["category"] for x in result["findings"]})
        self.assertIn("government_id", {x["category"] for x in result["findings"]})
        self.assertNotIn("GB82", repr(result))
        self.assertNotIn("123-45-6789", repr(result))

    def test_common_provider_tokens_and_labeled_secrets_are_detected(self):
        policy = SensitiveDataPolicy()
        for value in (
            "api_key=not-a-real-but-long-secret",
            "hf_abcdefghijklmnop1234",
            "AKIA1234567890ABCDEF",
            "client_secret=client-private-secret",
            "token=jwtvalue",
        ):
            with self.subTest(value=value):
                result = policy.inspect({"options": value})
                self.assertFalse(result["allowed"])
                self.assertNotIn(value, repr(result))

    def test_clean_payload_is_allowed_and_dates_are_not_false_positives(self):
        result = SensitiveDataPolicy().inspect({
            "prompt": "A cinematic mountain landscape at dawn"
        })
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_CLEAR")
        self.assertEqual(result["findings"], [])
        self.assertEqual(
            SensitiveDataPolicy().inspect({"date": "2026-10-16"})["findings"],
            [],
        )

    def test_oversized_deep_and_cyclic_payloads_fail_closed(self):
        policy = SensitiveDataPolicy(max_text_chars=10, max_depth=2)
        self.assertEqual(
            policy.inspect({"prompt": "x" * 11})["status"],
            "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED",
        )
        self.assertFalse(policy.inspect({"a": {"b": {"c": "safe"}}})["allowed"])
        cyclic = {}
        cyclic["self"] = cyclic
        self.assertEqual(
            SensitiveDataPolicy().inspect(cyclic)["status"],
            "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED",
        )

    def test_node_and_findings_budgets_fail_closed_without_partial_findings(self):
        self.assertEqual(
            SensitiveDataPolicy(max_nodes=2).inspect({"a": 1, "b": 2, "c": 3})["status"],
            "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED",
        )
        result = SensitiveDataPolicy(max_findings=1).inspect({
            "email": "jane@example.com",
            "phone": "+1 (415) 555-0134",
        })
        self.assertEqual(result["status"], "SENSITIVE_DATA_SCAN_LIMIT_EXCEEDED")
        self.assertEqual(result["findings"], [])

    def test_invalid_policy_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(blocked_categories=("everything",))
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(max_nodes=0)
        with self.assertRaises(ValueError):
            SensitiveDataPolicy(max_findings=1001)


if __name__ == "__main__":
    unittest.main()
