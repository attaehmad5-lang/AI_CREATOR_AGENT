import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_unlock_manager import ProviderUnlockManager
from projects.provider_store.unlock_flow_manager import UnlockFlowManager


class ProviderUnlockFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"
        self.catalog_path.write_text(json.dumps({"providers": [
            {
                "id": "image-provider", "name": "Image Provider",
                "category": "image", "auth_type": "NONE",
                "connection_type": "API", "enabled": True,
                "subscription_required": True,
                "subscription_url": "https://provider.example.com/pricing",
                "official_api_url": "https://provider.example.com/docs",
                "allowed_hosts": ["api.example.com"],
                "status": "CONNECTED",
            },
            {
                "id": "disabled-provider", "name": "Disabled Provider",
                "category": "video", "auth_type": "NONE",
                "connection_type": "API", "enabled": False,
                "subscription_required": False,
                "allowed_hosts": ["api.example.com"],
            },
        ]}), encoding="utf-8")
        self.state_path.write_text(
            json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}),
            encoding="utf-8",
        )
        self.connection = ConnectionManager(self.catalog_path, self.state_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_connected_label_does_not_fake_runtime_verification(self):
        manager = ProviderUnlockManager(self.connection)
        result = manager.connection_info("image-provider")
        self.assertFalse(result["verified"])
        self.assertNotEqual(result["status"], "CONNECTED")

    def test_only_live_connection_verification_sets_verified_state(self):
        configured = self.connection.configure_provider(
            "image-provider", endpoint="https://api.example.com/health"
        )
        self.assertTrue(configured["saved"])
        verified = self.connection.verify_connectivity(
            "image-provider", lambda provider, **kwargs: {"ok": True}
        )
        self.assertTrue(verified["verified"])
        result = ProviderUnlockManager(self.connection).connection_info("image-provider")
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "CONNECTED")

    def test_unlock_flow_is_user_controlled_and_never_purchases_or_bypasses(self):
        flow = UnlockFlowManager(self.connection)
        prepared = flow.prepare("image-provider")
        self.assertEqual(prepared["status"], "READY_FOR_USER_ACTION")
        self.assertEqual(prepared["next_steps"], [
            "USER_SUBSCRIPTION_OR_ACCOUNT",
            "USER_CREDENTIAL_CONNECTION",
            "CONNECTION_VERIFICATION",
            "PROVIDER_ENABLE",
        ])
        self.assertFalse(prepared["auto_purchase"])
        self.assertFalse(prepared["subscription_bypass"])
        self.assertFalse(prepared["api_key_logging"])

    def test_disabled_and_unknown_providers_remain_blocked(self):
        manager = ProviderUnlockManager(self.connection)
        disabled = manager.connection_info("disabled-provider")
        self.assertEqual(disabled["status"], "DISABLED")
        self.assertFalse(disabled["verified"])
        self.assertEqual(manager.connection_info("missing")["status"], "NOT_FOUND")
        self.assertEqual(UnlockFlowManager(self.connection).prepare("missing")["status"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
