import unittest

from projects.provider_store.privacy_data_lifecycle import PrivacyDataLifecycleManager
from projects.provider_store.provider_service import ProviderService


class ConnectionStub:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "NOT_CONNECTED", "verified": False}


class PrivacyAdapterStub:
    def delete_subject_data(self, **kwargs):
        return {"confirmed": True, "status": "ALREADY_ABSENT", "deleted_count": 0}

    def purge_expired_data(self, **kwargs):
        return {"confirmed": True, "status": "ALREADY_EMPTY", "deleted_count": 0}


class PrivacyLifecycleServiceStatusTests(unittest.TestCase):
    def test_service_reports_not_configured_when_no_lifecycle_manager(self):
        service = ProviderService(ConnectionStub())
        self.assertEqual(
            service.get_privacy_lifecycle_status(),
            {
                "status": "PRIVACY_LIFECYCLE_NOT_CONFIGURED",
                "configured": False,
                "registered_store_count": 0,
                "durable_idempotency_required": False,
                "durable_idempotency_ready": False,
            },
        )

    def test_service_reports_only_bounded_registered_store_count(self):
        lifecycle = PrivacyDataLifecycleManager({"generation-assets": PrivacyAdapterStub()})
        service = ProviderService(ConnectionStub(), privacy_lifecycle_manager=lifecycle)
        self.assertEqual(
            service.get_privacy_lifecycle_status(),
            {
                "status": "PRIVACY_LIFECYCLE_READY",
                "configured": True,
                "registered_store_count": 1,
                "durable_idempotency_required": False,
                "durable_idempotency_ready": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
