"""Provider data handling policy tests."""
import unittest

from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy


class ProviderDataHandlingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ProviderDataHandlingPolicy({
            "image": {
                "allowed_categories": ["public", "user_content", "personal"],
                "max_retention_days": 30,
                "training_use_allowed": False,
                "source_url": "https://provider.example.com/privacy",
            }
        })

    def test_unknown_provider_fails_closed(self):
        result = self.policy.evaluate(
            provider_id="unknown", data_categories=["user_content"]
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "PROVIDER_DATA_POLICY_UNKNOWN")

    def test_disallowed_category_and_credentials_are_blocked(self):
        result = self.policy.evaluate(
            provider_id="image", data_categories=["financial"]
        )
        self.assertEqual(result["status"], "DATA_CATEGORY_NOT_ALLOWED")
        credential = self.policy.evaluate(
            provider_id="image", data_categories=["credential"]
        )
        self.assertEqual(credential["status"], "CREDENTIAL_DATA_NEVER_ALLOWED")

    def test_retention_limit_is_enforced(self):
        result = self.policy.evaluate(
            provider_id="image", data_categories=["user_content"],
            retention_days=31,
        )
        self.assertEqual(result["status"], "RETENTION_PERIOD_EXCEEDS_PROVIDER_POLICY")

    def test_training_use_requires_provider_permission_and_user_consent(self):
        denied = self.policy.evaluate(
            provider_id="image", data_categories=["user_content"],
            provider_training_use=True, user_training_consent=True,
        )
        self.assertEqual(denied["status"], "PROVIDER_TRAINING_USE_NOT_AUTHORIZED")
        allowed_policy = ProviderDataHandlingPolicy({
            "text": {
                "allowed_categories": ["user_content"],
                "max_retention_days": 7,
                "training_use_allowed": True,
                "source_url": "https://provider.example.com/terms",
            }
        })
        no_consent = allowed_policy.evaluate(
            provider_id="text", data_categories=["user_content"],
            provider_training_use=True, user_training_consent=False,
        )
        self.assertEqual(no_consent["status"], "PROVIDER_TRAINING_USE_NOT_AUTHORIZED")
        consented = allowed_policy.evaluate(
            provider_id="text", data_categories=["user_content"],
            provider_training_use=True, user_training_consent=True,
        )
        self.assertTrue(consented["allowed"])

    def test_invalid_policy_and_request_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderDataHandlingPolicy({"image": {
                "allowed_categories": ["anything"], "max_retention_days": 10,
                "training_use_allowed": False, "source_url": "http://insecure.example",
            }})
        self.assertEqual(self.policy.evaluate(
            provider_id="image", data_categories=["unknown"]
        )["status"], "INVALID_DATA_CATEGORIES")
        self.assertEqual(self.policy.evaluate(
            provider_id="image", data_categories=["user_content"], retention_days=True
        )["status"], "INVALID_RETENTION_PERIOD")


if __name__ == "__main__":
    unittest.main()
