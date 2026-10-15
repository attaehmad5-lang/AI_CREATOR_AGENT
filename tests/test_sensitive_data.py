import unittest

from projects.provider_store.sensitive_data import SensitiveDataGuard, SensitiveDataScanner


class SensitiveDataScannerTests(unittest.TestCase):
    def test_detects_secret_patterns_without_returning_matched_values(self):
        secret = "hf_" + "A" * 25
        value = {
            "prompt": "Please use this token: Bearer abcdefghijklmnop and -----BEGIN PRIVATE KEY-----",
            "nested": {"api_key": secret, "private_key": "-----BEGIN PRIVATE KEY-----"},
        }
        result = SensitiveDataScanner().scan(value)
        self.assertEqual(result["status"], "SENSITIVE_DATA_FOUND")
        categories = {item["category"] for item in result["findings"]}
        self.assertIn("BEARER_TOKEN", categories)
        self.assertIn("CREDENTIAL_FIELD", categories)
        self.assertIn("PRIVATE_KEY", categories)
        rendered = repr(result)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("abcdefghijklmnop", rendered)

    def test_detects_personal_data_fields_and_inline_patterns(self):
        result = SensitiveDataScanner().scan({
            "contact": "Email alice@example.com or call (415) 555-2671; SSN 123-45-6789",
            "ssn": "123-45-6789",
        })
        categories = {item["category"] for item in result["findings"]}
        self.assertIn("EMAIL_ADDRESS", categories)
        self.assertIn("PHONE_NUMBER", categories)
        self.assertIn("PERSONAL_DATA_FIELD", categories)
        self.assertIn("US_SSN", categories)

    def test_safe_data_is_clear(self):
        result = SensitiveDataScanner().scan({"prompt": "A cinematic landscape at sunrise"})
        self.assertEqual(result["status"], "CLEAR")
        self.assertEqual(result["finding_count"], 0)

    def test_scanner_fails_closed_on_depth_nodes_string_and_unknown_types(self):
        deep = {"a": {"b": {"c": "clear"}}}
        self.assertEqual(SensitiveDataScanner(max_depth=1).scan(deep)["status"], "SCAN_LIMIT_EXCEEDED")
        self.assertEqual(SensitiveDataScanner(max_nodes=1).scan({"a": "clear"})["status"], "SCAN_LIMIT_EXCEEDED")
        self.assertEqual(SensitiveDataScanner(max_string_length=3).scan("long string")["status"], "SCAN_LIMIT_EXCEEDED")
        self.assertEqual(SensitiveDataScanner().scan(object())["status"], "SCAN_LIMIT_EXCEEDED")

    def test_cycles_fail_closed(self):
        value = {}
        value["self"] = value
        self.assertEqual(SensitiveDataScanner().scan(value)["status"], "SCAN_LIMIT_EXCEEDED")

    def test_invalid_limits_are_rejected(self):
        for kwargs in (
            {"max_depth": 0}, {"max_nodes": True}, {"max_string_length": 0}, {"max_findings": 10001}
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SensitiveDataScanner(**kwargs)


class SensitiveDataGuardTests(unittest.TestCase):
    def test_block_mode_denies_before_data_can_be_forwarded(self):
        guard = SensitiveDataGuard(mode="block")
        result = guard.sanitize({"prompt": "Contact alice@example.com"})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn("alice@example.com", repr(result))

    def test_redact_mode_redacts_inline_and_sensitive_fields(self):
        guard = SensitiveDataGuard(mode="redact")
        result = guard.sanitize({
            "prompt": "Contact alice@example.com and use api_key=secretvalue",
            "credentials": {"access_token": "do-not-return-this"},
            "ordinary": "Keep this text",
        })
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "REDACTED")
        self.assertNotIn("alice@example.com", repr(result["value"]))
        self.assertNotIn("secretvalue", repr(result["value"]))
        self.assertNotIn("do-not-return-this", repr(result["value"]))
        self.assertEqual(result["value"]["ordinary"], "Keep this text")
        self.assertNotIn("alice@example.com", repr(result["scan"]))

    def test_redaction_does_not_mutate_caller_data(self):
        original = {"email": "alice@example.com", "items": ["phone (415) 555-2671"]}
        snapshot = {"email": "alice@example.com", "items": ["phone (415) 555-2671"]}
        result = SensitiveDataGuard(mode="redact").sanitize(original)
        self.assertTrue(result["allowed"])
        self.assertEqual(original, snapshot)
        self.assertNotEqual(result["value"], original)

    def test_redaction_limit_or_cycle_fails_closed(self):
        value = {}
        value["cycle"] = value
        result = SensitiveDataGuard(mode="redact").sanitize(value)
        self.assertFalse(result["allowed"])
        self.assertIn(result["status"], {"PRIVACY_SCAN_FAILED", "PRIVACY_REDACTION_FAILED"})

    def test_invalid_guard_mode_and_scanner_are_rejected(self):
        with self.assertRaises(ValueError):
            SensitiveDataGuard(mode="ignore")
        with self.assertRaises(ValueError):
            SensitiveDataGuard(scanner=object())


if __name__ == "__main__":
    unittest.main()
