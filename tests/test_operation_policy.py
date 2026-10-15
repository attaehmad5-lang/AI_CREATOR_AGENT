import unittest

from projects.provider_store.operation_policy import ProviderOperationPolicy


class ProviderOperationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ProviderOperationPolicy([
            {
                "subject_id": "user-a",
                "project_id": "film",
                "operation": "image.generate",
                "provider_ids": ["fallback"],
            },
            {
                "subject_id": "user-a",
                "project_id": "film",
                "operation": "image.edit",
                "provider_ids": ["primary"],
            },
        ])

    def test_only_explicit_subject_project_operation_provider_grants_are_returned(self):
        allowed = self.policy.allowed_provider_ids(
            subject_id="user-a", project_id="film", operation="image.generate"
        )
        self.assertEqual(allowed, {
            "status": "ACCESS_POLICY_FOUND", "provider_ids": ["fallback"]
        })

    def test_missing_or_mismatched_grant_denies_by_default(self):
        for args in (
            {"subject_id": "user-b", "project_id": "film", "operation": "image.generate"},
            {"subject_id": "user-a", "project_id": "other", "operation": "image.generate"},
            {"subject_id": "user-a", "project_id": "film", "operation": "video.generate"},
        ):
            self.assertEqual(
                self.policy.allowed_provider_ids(**args)["status"], "ACCESS_DENIED"
            )

    def test_wildcards_and_invalid_grant_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderOperationPolicy([{
                "subject_id": "*", "project_id": "film",
                "operation": "image.generate", "provider_ids": ["primary"],
            }])
        with self.assertRaises(ValueError):
            ProviderOperationPolicy([{
                "subject_id": "user-a", "project_id": "film",
                "operation": "image.generate", "provider_ids": ["*"],
            }])
        with self.assertRaises(ValueError):
            ProviderOperationPolicy([{
                "subject_id": "user-a", "project_id": "film",
                "operation": "IMAGE GENERATE", "provider_ids": ["primary"],
            }])

    def test_empty_grant_list_denies_everyone(self):
        policy = ProviderOperationPolicy([])
        self.assertEqual(
            policy.allowed_provider_ids(
                subject_id="user-a", project_id="film", operation="image.generate"
            )["status"],
            "ACCESS_DENIED",
        )


if __name__ == "__main__":
    unittest.main()
