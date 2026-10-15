import unittest
from datetime import datetime, timedelta, timezone

from projects.provider_store.data_retention import ProviderDataRetentionPolicy


class ProviderDataRetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ProviderDataRetentionPolicy({
            "provider-a": {
                "allowed_data_categories": ["public", "personal"],
                "allowed_purposes": ["generation", "translation"],
                "max_retention_days": 30,
                "uses_data_for_training": True,
            },
            "provider-b": {
                "allowed_data_categories": ["public"],
                "allowed_purposes": ["generation"],
                "max_retention_days": 0,
                "uses_data_for_training": False,
            },
        })

    def test_policy_allows_only_explicit_categories_purposes_and_retention(self):
        result = self.policy.evaluate(
            "provider-a", data_categories=["personal"], purpose="translation",
            retention_days=14, training_opt_in=True,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["status"], "DATA_POLICY_ALLOWED")

    def test_unknown_provider_and_undeclared_data_are_denied(self):
        self.assertEqual(self.policy.evaluate(
            "unknown", data_categories=["public"], purpose="generation",
            retention_days=0,
        )["status"], "DATA_HANDLING_POLICY_NOT_CONFIGURED")
        self.assertEqual(self.policy.evaluate(
            "provider-b", data_categories=["personal"], purpose="generation",
            retention_days=0,
        )["status"], "DATA_CATEGORY_NOT_ALLOWED")

    def test_credentials_are_never_accepted_as_content_categories(self):
        self.assertEqual(self.policy.evaluate(
            "provider-a", data_categories=["credentials"], purpose="generation",
            retention_days=0, training_opt_in=True,
        )["status"], "CREDENTIAL_DATA_FORBIDDEN")
        with self.assertRaises(ValueError):
            ProviderDataRetentionPolicy({
                "provider-a": {
                    "allowed_data_categories": ["credentials"],
                    "allowed_purposes": ["generation"],
                    "max_retention_days": 1,
                    "uses_data_for_training": False,
                }
            })

    def test_training_requires_explicit_consent(self):
        result = self.policy.evaluate(
            "provider-a", data_categories=["public"], purpose="generation",
            retention_days=0, training_opt_in=False,
        )
        self.assertEqual(result["status"], "TRAINING_CONSENT_REQUIRED")
        allowed = self.policy.evaluate(
            "provider-b", data_categories=["public"], purpose="generation",
            retention_days=0, training_opt_in=False,
        )
        self.assertTrue(allowed["allowed"])

    def test_retention_period_and_invalid_arguments_fail_closed(self):
        self.assertEqual(self.policy.evaluate(
            "provider-b", data_categories=["public"], purpose="generation",
            retention_days=1,
        )["status"], "RETENTION_PERIOD_EXCEEDED")
        self.assertEqual(self.policy.evaluate(
            "provider-b", data_categories=["public"], purpose="unknown",
            retention_days=0,
        )["status"], "DATA_PURPOSE_NOT_ALLOWED")
        self.assertFalse(self.policy.evaluate(
            "provider-b", data_categories=["public"], purpose="generation",
            retention_days=True,
        )["allowed"])
        self.assertFalse(self.policy.evaluate(
            "provider-b", data_categories=["public"], purpose="generation",
            retention_days=0, training_opt_in="yes",
        )["allowed"])

    def test_policy_configuration_rejects_invalid_types_and_limits(self):
        invalid = [
            {"allowed_data_categories": "public", "allowed_purposes": ["generation"], "max_retention_days": 1, "uses_data_for_training": False},
            {"allowed_data_categories": ["public"], "allowed_purposes": [], "max_retention_days": 1, "uses_data_for_training": False},
            {"allowed_data_categories": ["public"], "allowed_purposes": ["generation"], "max_retention_days": True, "uses_data_for_training": False},
            {"allowed_data_categories": ["public"], "allowed_purposes": ["generation"], "max_retention_days": 1, "uses_data_for_training": "false"},
        ]
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                ProviderDataRetentionPolicy({"provider-a": policy})

    def test_retention_deadline_requires_timezone_and_bounded_days(self):
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            ProviderDataRetentionPolicy.retention_deadline(created, 2),
            (created + timedelta(days=2)).isoformat(),
        )
        with self.assertRaises(ValueError):
            ProviderDataRetentionPolicy.retention_deadline("2026-01-01T00:00:00", 2)
        with self.assertRaises(ValueError):
            ProviderDataRetentionPolicy.retention_deadline(created, True)

    def test_purge_expired_records_drops_expired_and_malformed_timestamps(self):
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        result = ProviderDataRetentionPolicy.purge_expired_records(
            [
                {"id": "expired", "created_at": "2026-01-01T00:00:00Z"},
                {"id": "kept", "created_at": "2026-01-09T00:00:00Z"},
                {"id": "bad", "created_at": "not-a-date"},
                {"id": "missing"},
            ],
            now=now, retention_days=2,
        )
        self.assertEqual([item["id"] for item in result["kept"]], ["kept"])
        self.assertEqual(result["purged"], 3)
        self.assertEqual(result["invalid_timestamp_purged"], 2)

    def test_purge_fails_closed_on_invalid_collection_time_or_window(self):
        self.assertEqual(
            ProviderDataRetentionPolicy.purge_expired_records(
                {}, now=datetime.now(timezone.utc), retention_days=1
            )["status"],
            "INVALID_RECORDS",
        )
        self.assertEqual(
            ProviderDataRetentionPolicy.purge_expired_records(
                [], now=datetime(2026, 1, 1), retention_days=1
            )["status"],
            "INVALID_CURRENT_TIME",
        )
        self.assertEqual(
            ProviderDataRetentionPolicy.purge_expired_records(
                [], now="not-a-datetime", retention_days=1
            )["status"],
            "INVALID_CURRENT_TIME",
        )
        self.assertEqual(
            ProviderDataRetentionPolicy.purge_expired_records(
                [], now=datetime.now(timezone.utc), retention_days=True
            )["status"],
            "INVALID_RETENTION_PERIOD",
        )


if __name__ == "__main__":
    unittest.main()
