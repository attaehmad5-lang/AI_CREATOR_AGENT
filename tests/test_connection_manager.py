import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager


class ConnectionManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "provider_catalog.json"
        self.state_path = self.root / "connection_state.json"
        self.catalog_path.write_text(
            json.dumps({
                "providers": [
                    {"id": "public_image", "auth_type": "NONE", "category": "image"},
                    {"id": "token_image", "auth_type": "API_TOKEN", "token_env": "TEST_PROVIDER_TOKEN", "category": "image"},
                ]
            }),
            encoding="utf-8",
        )
        self.state_path.write_text(
            json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}),
            encoding="utf-8",
        )
        self.manager = ConnectionManager(self.catalog_path, self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_loads_catalog_and_unknown_state(self):
        self.assertTrue(self.manager.provider_exists("public_image"))
        self.assertEqual(self.manager.get_state("public_image")["status"], "UNKNOWN")
        self.assertEqual(len(self.manager.list_providers()), 2)

    def test_rejects_unknown_provider_and_invalid_state(self):
        self.assertEqual(self.manager.set_state("missing", "READY")["status"], "NOT_FOUND")
        self.assertEqual(self.manager.set_state("public_image", "NOT_A_STATE")["status"], "INVALID_STATE")

    def test_connected_requires_verification(self):
        result = self.manager.set_state("public_image", "CONNECTED")
        self.assertEqual(result["status"], "VERIFICATION_REQUIRED")
        self.assertFalse(result["saved"])
        self.assertEqual(self.manager.get_state("public_image")["status"], "UNKNOWN")

    def test_state_is_persisted_atomically_and_reloadable(self):
        result = self.manager.set_state("public_image", "READY")
        self.assertTrue(result["saved"])
        reloaded = ConnectionManager(self.catalog_path, self.state_path)
        self.assertEqual(reloaded.get_state("public_image")["status"], "READY")
        self.assertIn("updated_at", reloaded.get_state("public_image"))

    def test_credential_check_never_returns_secret(self):
        secret = "do-not-leak-this-token"
        result = self.manager.credential_status(
            "token_image", {"TEST_PROVIDER_TOKEN": secret}
        )
        self.assertEqual(result["status"], "CREDENTIALS_PRESENT")
        self.assertTrue(result["credential_present"])
        self.assertNotIn(secret, repr(result))

    def test_public_provider_needs_no_credential(self):
        result = self.manager.credential_status("public_image", {})
        self.assertEqual(result["status"], "NO_CREDENTIAL_REQUIRED")

    def test_summary_handles_corrupt_status_without_crashing(self):
        self.manager.state["providers"]["token_image"] = {"status": "unexpected"}
        self.assertEqual(self.manager.summary()["UNKNOWN"], 1)

    def test_legacy_connected_state_is_not_treated_as_verified(self):
        self.manager.state["providers"]["token_image"] = {"status": "CONNECTED"}
        state = self.manager.get_state("token_image")
        self.assertEqual(state["status"], "UNKNOWN")
        self.assertEqual(state["reported_status"], "CONNECTED")
        self.assertFalse(state["verified"])
        self.assertEqual(self.manager.summary()["UNKNOWN"], 1)

    def test_register_provider_persists_valid_metadata(self):
        provider = {
            "id": "local_image", "name": "Local Image", "category": "image",
            "auth_type": "NONE", "connection_type": "LOCAL",
        }
        result = self.manager.register_provider(provider)
        self.assertEqual(result["status"], "REGISTERED")
        reloaded = ConnectionManager(self.catalog_path, self.state_path)
        self.assertEqual(reloaded.get_provider("local_image")["name"], "Local Image")

    def test_register_provider_rejects_duplicate_without_overwrite(self):
        provider = {
            "id": "token_image", "name": "Replacement", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API",
        }
        result = self.manager.register_provider(provider)
        self.assertEqual(result["status"], "ALREADY_EXISTS")
        self.assertEqual(self.manager.get_provider("token_image")["auth_type"], "API_TOKEN")

    def test_register_provider_rejects_missing_contract_fields(self):
        result = self.manager.register_provider({"id": "broken"})
        self.assertEqual(result["status"], "INVALID_PROVIDER")
        self.assertFalse(result.get("saved", False))

    def test_register_provider_rejects_secret_values_in_metadata(self):
        provider = {
            "id": "unsafe", "name": "Unsafe", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API",
            "api_key": "never-store-this",
        }
        result = self.manager.register_provider(provider)
        self.assertEqual(result["status"], "SENSITIVE_METADATA_REJECTED")
        self.assertFalse(self.manager.provider_exists("unsafe"))

    def test_register_provider_rejects_invalid_id(self):
        provider = {
            "id": "../unsafe", "name": "Unsafe", "category": "image",
            "auth_type": "NONE", "connection_type": "LOCAL",
        }
        self.assertEqual(self.manager.register_provider(provider)["status"], "INVALID_PROVIDER")

    def test_discovery_filters_by_category_case_insensitively(self):
        results = self.manager.discover_providers(category="IMAGE")
        self.assertEqual({item["id"] for item in results}, {"public_image", "token_image"})

    def test_discovery_filters_by_capability(self):
        self.manager.catalog["providers"][0]["capabilities"] = ["generate", "edit"]
        results = self.manager.discover_providers(capability="EDIT")
        self.assertEqual([item["id"] for item in results], ["public_image"])

    def test_discovery_enabled_only_excludes_unspecified_and_disabled(self):
        self.manager.catalog["providers"][0]["enabled"] = True
        results = self.manager.discover_providers(enabled_only=True)
        self.assertEqual([item["id"] for item in results], ["public_image"])

    def test_load_provider_uses_factory_and_keeps_instance_in_memory(self):
        class ImageProvider:
            def generate_image(self, prompt, output_path):
                return None
        instance = ImageProvider()
        result = self.manager.load_provider("public_image", lambda metadata: instance)
        self.assertEqual(result["status"], "LOADED")
        self.assertIs(self.manager.get_loaded_provider("public_image"), instance)
        self.assertNotIn("public_image", self.manager.state["providers"])

    def test_load_provider_rejects_disabled_provider(self):
        self.manager.catalog["providers"][0]["enabled"] = False
        result = self.manager.load_provider("public_image", lambda metadata: object())
        self.assertEqual(result["status"], "DISABLED")
        self.assertIsNone(self.manager.get_loaded_provider("public_image"))

    def test_load_provider_does_not_leak_factory_exception_text(self):
        secret = "private-token-sample"
        def broken_factory(metadata):
            raise RuntimeError(secret)
        result = self.manager.load_provider("public_image", broken_factory)
        self.assertEqual(result["status"], "LOAD_FAILED")
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result["error_type"], "RuntimeError")

    def test_load_provider_rejects_missing_factory_and_unknown_provider(self):
        self.assertEqual(
            self.manager.load_provider("public_image", None)["status"], "INVALID_FACTORY"
        )
        self.assertEqual(
            self.manager.load_provider("not_registered", lambda metadata: object())["status"],
            "NOT_FOUND",
        )

    def test_provider_contract_accepts_required_modality_method(self):
        class ImageProvider:
            def generate_image(self, prompt, output_path):
                return None
        result = self.manager.validate_provider_contract("public_image", ImageProvider())
        self.assertTrue(result["valid"])
        self.assertEqual(result["contract_version"], "1.0")

    def test_provider_contract_rejects_missing_method_and_prevents_loading(self):
        instance = object()
        contract = self.manager.validate_provider_contract("public_image", instance)
        self.assertEqual(contract["status"], "CONTRACT_VIOLATION")
        loaded = self.manager.load_provider("public_image", lambda metadata: instance)
        self.assertEqual(loaded["status"], "CONTRACT_VIOLATION")
        self.assertIsNone(self.manager.get_loaded_provider("public_image"))

    def test_provider_contract_handles_unknown_modality_explicitly(self):
        self.manager.catalog["providers"][0]["category"] = "quantum"
        result = self.manager.validate_provider_contract("public_image", object())
        self.assertEqual(result["status"], "UNSUPPORTED_MODALITY")

    def test_unload_provider_calls_close_and_removes_instance(self):
        class Closable:
            def __init__(self):
                self.closed = False
            def generate_image(self, prompt, output_path):
                return None
            def close(self):
                self.closed = True
        instance = Closable()
        self.manager.load_provider("public_image", lambda metadata: instance)
        result = self.manager.unload_provider("public_image")
        self.assertEqual(result["status"], "UNLOADED")
        self.assertTrue(instance.closed)
        self.assertIsNone(self.manager.get_loaded_provider("public_image"))

    def test_unload_provider_preserves_instance_if_cleanup_fails(self):
        class BrokenCleanup:
            def generate_image(self, prompt, output_path):
                return None
            def close(self):
                raise RuntimeError("sensitive endpoint details")
        instance = BrokenCleanup()
        self.manager.load_provider("public_image", lambda metadata: instance)
        result = self.manager.unload_provider("public_image")
        self.assertEqual(result["status"], "UNLOAD_FAILED")
        self.assertNotIn("sensitive endpoint details", repr(result))
        self.assertIs(self.manager.get_loaded_provider("public_image"), instance)

    def test_unload_provider_reports_not_loaded_and_unknown(self):
        self.assertEqual(self.manager.unload_provider("public_image")["status"], "NOT_LOADED")
        self.assertEqual(self.manager.unload_provider("missing")["status"], "NOT_FOUND")

    def test_enable_disable_persists_catalog_flag_without_claiming_connection(self):
        result = self.manager.set_provider_enabled("public_image", False)
        self.assertEqual(result["status"], "DISABLED")
        self.assertFalse(self.manager.is_provider_enabled("public_image"))
        self.assertEqual(self.manager.get_state("public_image")["status"], "UNKNOWN")
        reloaded = ConnectionManager(self.catalog_path, self.state_path)
        self.assertFalse(reloaded.is_provider_enabled("public_image"))
        enabled = reloaded.set_provider_enabled("public_image", True)
        self.assertEqual(enabled["status"], "ENABLED")
        self.assertTrue(reloaded.is_provider_enabled("public_image"))
        self.assertIn("no purchase", enabled["note"].lower())

    def test_disabling_unloads_runtime_instance(self):
        class Closable:
            def __init__(self):
                self.closed = False
            def generate_image(self, prompt, output_path):
                return None
            def close(self):
                self.closed = True
        instance = Closable()
        self.manager.load_provider("public_image", lambda metadata: instance)
        result = self.manager.set_provider_enabled("public_image", False)
        self.assertEqual(result["status"], "DISABLED")
        self.assertTrue(instance.closed)
        self.assertIsNone(self.manager.get_loaded_provider("public_image"))

    def test_enabled_flag_rejects_non_boolean_and_unknown_provider(self):
        self.assertEqual(
            self.manager.set_provider_enabled("public_image", "yes")["status"],
            "INVALID_ENABLED_VALUE",
        )
        self.assertEqual(
            self.manager.set_provider_enabled("missing", True)["status"], "NOT_FOUND"
        )

    def test_metadata_returns_detached_copy_and_filters_sensitive_fields(self):
        self.manager.catalog["providers"][0]["official_api_url"] = "https://example.invalid"
        self.manager.catalog["providers"][0]["token_env"] = "PUBLIC_IMAGE_TOKEN"
        self.manager.catalog["providers"][0]["access_token"] = "must-not-appear"
        metadata = self.manager.get_provider_metadata("public_image")
        self.assertEqual(metadata["official_api_url"], "https://example.invalid")
        self.assertEqual(metadata["token_env"], "PUBLIC_IMAGE_TOKEN")
        self.assertNotIn("access_token", metadata)
        metadata["name"] = "mutated"
        self.assertNotEqual(self.manager.get_provider("public_image").get("name"), "mutated")

    def test_metadata_returns_none_for_unknown_provider(self):
        self.assertIsNone(self.manager.get_provider_metadata("not_registered"))


if __name__ == "__main__":
    unittest.main()
