import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager


def _set_provider_state_in_process(args):
    catalog_path, state_path, provider_id = args
    manager = ConnectionManager(catalog_path, state_path)
    return manager.set_state(provider_id, "READY")["saved"]


def _register_provider_in_process(args):
    catalog_path, state_path, index = args
    manager = ConnectionManager(catalog_path, state_path)
    result = manager.register_provider({
        "id": f"process_provider_{index}",
        "name": f"Process Provider {index}",
        "category": "image",
        "auth_type": "NONE",
        "connection_type": "API",
        "enabled": True,
    })
    return result["status"]


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

    def test_concurrent_connection_state_updates_across_processes_preserve_records(self):
        catalog_path = self.root / "process_state_catalog.json"
        state_path = self.root / "process_state.json"
        catalog = {
            "providers": [
                {
                    "id": f"state_provider_{index}",
                    "name": f"State Provider {index}",
                    "category": "image",
                    "auth_type": "NONE",
                    "connection_type": "API",
                    "enabled": True,
                }
                for index in range(20)
            ]
        }
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        state_path.write_text(
            json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}),
            encoding="utf-8",
        )
        args = [
            (str(catalog_path), str(state_path), f"state_provider_{index}")
            for index in range(20)
        ]
        with ProcessPoolExecutor(max_workers=4) as pool:
            saved = list(pool.map(_set_provider_state_in_process, args))

        self.assertTrue(all(saved))
        loaded = ConnectionManager(catalog_path, state_path)
        self.assertEqual(len(loaded.state["providers"]), 20)
        self.assertTrue(all(
            loaded.get_state(f"state_provider_{index}")["status"] == "READY"
            for index in range(20)
        ))

    def test_concurrent_catalog_registration_across_processes_preserves_all_providers(self):
        catalog_path = self.root / "process_registration_catalog.json"
        state_path = self.root / "process_registration_state.json"
        catalog_path.write_text(
            json.dumps({"providers": []}), encoding="utf-8"
        )
        state_path.write_text(
            json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}),
            encoding="utf-8",
        )
        args = [
            (str(catalog_path), str(state_path), index)
            for index in range(20)
        ]
        with ProcessPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_register_provider_in_process, args))

        self.assertEqual(statuses.count("REGISTERED"), 20)
        loaded = ConnectionManager(catalog_path, state_path)
        self.assertEqual(len(loaded.list_providers()), 20)
        self.assertEqual(len({p["id"] for p in loaded.list_providers()}), 20)

    def test_loads_catalog_and_unknown_state(self):
        self.assertTrue(self.manager.provider_exists("public_image"))
        self.assertEqual(self.manager.get_state("public_image")["status"], "UNKNOWN")
        self.assertEqual(len(self.manager.list_providers()), 2)

    def test_catalog_schema_validation_detects_duplicate_ids_and_secret_fields(self):
        result = ConnectionManager.validate_catalog_schema({
            "providers": [
                {"id": "same", "api_token": "must-not-be-here"},
                {"id": "same", "enabled": "yes"},
            ]
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("duplicate" in error for error in result["errors"]))
        self.assertTrue(any("credential value" in error for error in result["errors"]))
        self.assertTrue(any("enabled must be boolean" in error for error in result["errors"]))

    def test_catalog_schema_rejects_secret_values_in_environment_reference_fields(self):
        for field, value in (
            ("token_env", "secret-token-value"),
            ("api_key_env", "API_KEY\nINJECTED"),
            ("token_expiry_env", "TOKEN=private"),
            ("credential_env", ""),
        ):
            with self.subTest(field=field):
                result = ConnectionManager.validate_catalog_schema({
                    "providers": [{"id": "provider", field: value}]
                })
                self.assertFalse(result["valid"])
                self.assertTrue(any(field in error for error in result["errors"]))

        valid = ConnectionManager.validate_catalog_schema({
            "providers": [{"id": "provider", "token_env": "HF_TOKEN",
                           "token_expiry_env": "HF_TOKEN_EXPIRES"}]
        })
        self.assertTrue(valid["valid"], valid["errors"])

    def test_catalog_schema_requires_boolean_subscription_flag(self):
        result = ConnectionManager.validate_catalog_schema({
            "providers": [{"id": "paid", "subscription_required": "true"}]
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("subscription_required must be boolean" in error for error in result["errors"]))

    def test_constructor_rejects_invalid_catalog_schema(self):
        bad_catalog = self.root / "bad_catalog.json"
        bad_catalog.write_text(json.dumps({"providers": "not-a-list"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid provider catalog"):
            ConnectionManager(bad_catalog, self.state_path)

    def test_production_catalog_api_allowlists_are_explicit_and_valid(self):
        catalog_path = Path(__file__).resolve().parents[1] / "projects" / "provider_store" / "provider_catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        providers = catalog["providers"]
        allowlisted = [provider for provider in providers if "allowed_hosts" in provider]
        self.assertGreaterEqual(len(allowlisted), 25)
        for provider in allowlisted:
            with self.subTest(provider=provider.get("id")):
                hosts = provider["allowed_hosts"]
                self.assertIsInstance(hosts, list)
                self.assertTrue(hosts)
                self.assertEqual(len(hosts), len(set(hosts)))
                for host in hosts:
                    self.assertIsInstance(host, str)
                    self.assertTrue(host)
                    self.assertNotIn("://", host)
                    validation = ConnectionManager.validate_endpoint(
                        f"https://{host}/health", allowed_hosts={host}
                    )
                    self.assertTrue(validation["valid"], validation)

    def test_catalog_schema_accepts_existing_registry_shape(self):
        result = ConnectionManager.validate_catalog_schema(self.manager.catalog)
        self.assertTrue(result["valid"], result["errors"])

    def test_rejects_unknown_provider_and_invalid_state(self):
        self.assertEqual(self.manager.set_state("missing", "READY")["status"], "NOT_FOUND")
        self.assertEqual(self.manager.set_state("public_image", "NOT_A_STATE")["status"], "INVALID_STATE")
        self.assertEqual(
            self.manager.set_state("public_image", "CONNECTED", verified="true")["status"],
            "INVALID_VERIFICATION_FLAG",
        )
        self.assertEqual(
            self.manager.set_state("public_image", "CONNECTED", verified=1)["status"],
            "INVALID_VERIFICATION_FLAG",
        )

    def test_connected_requires_verification(self):
        result = self.manager.set_state("public_image", "CONNECTED")
        self.assertEqual(result["status"], "VERIFICATION_REQUIRED")
        self.assertFalse(result["saved"])
        self.assertEqual(self.manager.get_state("public_image")["status"], "UNKNOWN")

    def test_public_state_setter_cannot_mint_verified_connection(self):
        for state in ("CONNECTED", "READY"):
            with self.subTest(state=state):
                result = self.manager.set_state("public_image", state, verified=True)
                self.assertEqual(result["status"], "VERIFICATION_REQUIRED")
                self.assertFalse(result["saved"])
                current = self.manager.get_state("public_image")
                self.assertEqual(current["status"], "UNKNOWN")
                self.assertFalse(current["verified"])

    def test_state_is_persisted_atomically_and_reloadable(self):
        result = self.manager.set_state("public_image", "READY")
        self.assertTrue(result["saved"])
        reloaded = ConnectionManager(self.catalog_path, self.state_path)
        self.assertEqual(reloaded.get_state("public_image")["status"], "READY")
        self.assertIn("updated_at", reloaded.get_state("public_image"))

    def test_loaded_state_is_schema_checked_and_secrets_are_redacted(self):
        self.state_path.write_text(
            json.dumps({
                "version": "1.0",
                "providers": {
                    "public_image": {
                        "status": "READY",
                        "configuration": {
                            "model": "safe-model",
                            "api_key": "do-not-return",
                            "nested": {"refresh_token": "also-secret", "region": "global"},
                        },
                    }
                },
            }),
            encoding="utf-8",
        )
        manager = ConnectionManager(self.catalog_path, self.state_path)
        visible = manager.get_state("public_image")
        self.assertEqual(visible["configuration"]["model"], "safe-model")
        self.assertEqual(visible["configuration"]["nested"]["region"], "global")
        self.assertNotIn("api_key", repr(visible))
        self.assertNotIn("refresh_token", repr(visible))
        self.assertNotIn("do-not-return", repr(visible))
        self.assertNotIn("also-secret", repr(visible))

    def test_invalid_connection_state_schema_is_rejected(self):
        self.state_path.write_text(json.dumps({"providers": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            ConnectionManager(self.catalog_path, self.state_path)

    def test_state_updates_preserve_existing_nonsecret_configuration(self):
        self.manager.state["providers"]["public_image"] = {
            "status": "READY", "configuration": {"model": "local-test"},
            "last_success_at": "2026-01-01T00:00:00+00:00",
        }
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
        self.manager.set_state("public_image", "ERROR")
        state = self.manager.get_state("public_image")
        self.assertEqual(state["configuration"]["model"], "local-test")
        self.assertEqual(state["last_success_at"], "2026-01-01T00:00:00+00:00")

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

    def test_credential_expiry_detection_never_exposes_token(self):
        self.manager.catalog["providers"][1]["token_expiry_env"] = "TEST_PROVIDER_TOKEN_EXPIRES"
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        secret = "do-not-print-this-token"
        expired = self.manager.credential_status(
            "token_image",
            {"TEST_PROVIDER_TOKEN": secret, "TEST_PROVIDER_TOKEN_EXPIRES": "2000-01-01T00:00:00Z"},
        )
        self.assertEqual(expired["status"], "CREDENTIALS_EXPIRED")
        self.assertNotIn(secret, repr(expired))
        invalid = self.manager.credential_status(
            "token_image",
            {"TEST_PROVIDER_TOKEN": secret, "TEST_PROVIDER_TOKEN_EXPIRES": "not-a-date"},
        )
        self.assertEqual(invalid["status"], "CREDENTIAL_EXPIRY_INVALID")

    def test_credential_expiring_soon_is_flagged(self):
        from datetime import datetime, timedelta, timezone
        self.manager.catalog["providers"][1]["token_expiry_env"] = "TEST_PROVIDER_TOKEN_EXPIRES"
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        soon = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        result = self.manager.credential_status(
            "token_image",
            {"TEST_PROVIDER_TOKEN": "secret-value", "TEST_PROVIDER_TOKEN_EXPIRES": soon},
        )
        self.assertEqual(result["status"], "CREDENTIALS_EXPIRING")

    def test_configured_endpoint_and_model_are_passed_to_probe(self):
        self.manager.catalog["providers"][0]["allowed_hosts"] = ["api.example.com"]
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        configured = self.manager.configure_provider(
            "public_image", endpoint="https://api.example.com/v1/models", model="image-v1"
        )
        self.assertTrue(configured["saved"])
        seen = []
        result = self.manager.verify_connectivity(
            "public_image",
            lambda provider, **kwargs: seen.append(provider.get("configuration")) or {"ok": True},
        )
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(seen[0]["endpoint"], "https://api.example.com/v1/models")
        self.assertEqual(seen[0]["model"], "image-v1")
        self.manager.state["providers"]["public_image"]["configuration"]["endpoint"] = "https://evil.example.com/models"
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
        called = []
        rejected = self.manager.verify_connectivity(
            "public_image",
            lambda provider, **kwargs: called.append(True) or {"ok": True},
        )
        self.assertNotEqual(rejected["status"], "CONNECTED")
        self.assertEqual(called, [])

    def test_persisted_endpoint_without_allowlist_is_rejected_before_probe(self):
        self.manager.state["providers"]["public_image"] = {
            "status": "CONNECTED", "verified": True,
            "configuration": {"endpoint": "https://api.example.com/health"},
        }
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
        called = []
        result = self.manager.verify_connectivity(
            "public_image",
            lambda provider, **kwargs: called.append(True) or {"ok": True},
        )
        self.assertEqual(result["status"], "HOST_ALLOWLIST_REQUIRED")
        self.assertFalse(result["verified"])
        self.assertEqual(called, [])
        state = self.manager.get_state("public_image")
        self.assertEqual(state["status"], "ERROR")
        self.assertFalse(state["verified"])

    def test_catalog_provider_credentials_resolve_from_environment_references(self):
        catalog_path = Path(__file__).resolve().parents[1] / "projects" / "provider_store" / "provider_catalog.json"
        manager = ConnectionManager(catalog_path, self.state_path)
        status = manager.credential_status("google_image", {"GOOGLE_API_KEY": "test-only-secret"})
        self.assertEqual(status["status"], "CREDENTIALS_PRESENT")
        self.assertEqual(status["credential_name"], "GOOGLE_API_KEY")
        self.assertNotIn("test-only-secret", repr(status))
        missing = manager.credential_status("google_image", {})
        self.assertEqual(missing["status"], "CREDENTIALS_MISSING")

    def test_connectivity_fails_closed_when_state_json_is_corrupt(self):
        self.state_path.write_text("{corrupt-json", encoding="utf-8")
        called = []
        result = self.manager.verify_connectivity(
            "public_image",
            lambda provider, **kwargs: called.append(True) or {"ok": True},
        )
        self.assertEqual(result["status"], "STATE_READ_FAILED")
        self.assertFalse(result["verified"])
        self.assertEqual(called, [])

    def test_persisted_secret_bearing_model_is_rejected_before_probe(self):
        self.manager.catalog["providers"][0]["allowed_hosts"] = ["api.example.com"]
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        self.manager.state["providers"]["public_image"] = {
            "status": "CONNECTED", "verified": True,
            "configuration": {
                "endpoint": "https://api.example.com/health",
                "model": "api_key=private-value",
            },
        }
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
        called = []
        result = self.manager.verify_connectivity(
            "public_image",
            lambda provider, **kwargs: called.append(True) or {"ok": True},
        )
        self.assertEqual(result["status"], "SENSITIVE_CONFIGURATION_REJECTED")
        self.assertFalse(result["verified"])
        self.assertEqual(called, [])

    def test_connectivity_never_probes_with_expired_credentials(self):
        self.manager.catalog["providers"][1]["token_expiry_env"] = "TEST_PROVIDER_TOKEN_EXPIRES"
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        connected = self.manager.verify_connectivity(
            "token_image",
            lambda provider, **kwargs: {"ok": True},
            environ={
                "TEST_PROVIDER_TOKEN": "private-token",
                "TEST_PROVIDER_TOKEN_EXPIRES": "2099-01-01T00:00:00Z",
            },
        )
        self.assertEqual(connected["status"], "CONNECTED")
        called = []
        result = self.manager.verify_connectivity(
            "token_image",
            lambda *args, **kwargs: called.append(True),
            environ={
                "TEST_PROVIDER_TOKEN": "private-token",
                "TEST_PROVIDER_TOKEN_EXPIRES": "2000-01-01T00:00:00Z",
            },
        )
        self.assertEqual(result["status"], "CREDENTIALS_EXPIRED")
        self.assertEqual(called, [])
        state = self.manager.get_state("token_image")
        self.assertEqual(state["status"], "ERROR")
        self.assertFalse(state["verified"])

    def test_summary_handles_corrupt_status_without_crashing(self):
        self.manager.state["providers"]["token_image"] = {"status": "unexpected"}
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
        self.assertEqual(self.manager.summary()["UNKNOWN"], 1)

    def test_legacy_connected_state_is_not_treated_as_verified(self):
        self.manager.state["providers"]["token_image"] = {"status": "CONNECTED"}
        self.state_path.write_text(json.dumps(self.manager.state), encoding="utf-8")
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

    def test_catalog_validator_detects_nested_credential_values(self):
        result = ConnectionManager.validate_catalog_schema({
            "providers": [{
                "id": "nested_secret",
                "metadata": {"auth": {"refresh_token": "must-not-be-here"}},
            }]
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("credential value" in error for error in result["errors"]))
        for secret_key in ("client_secret", "db_password", "authorization_header", "session_token", "oauth_token", "id_token", "session_cookie", "signing_key"):
            result = ConnectionManager.validate_catalog_schema({
                "providers": [{
                    "id": "compound_secret",
                    "metadata": {secret_key: "must-not-be-stored"},
                }]
            })
            self.assertFalse(result["valid"], secret_key)

    def test_register_provider_rejects_secret_values_in_metadata(self):
        provider = {
            "id": "unsafe", "name": "Unsafe", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API",
            "api_key": "never-store-this",
        }
        result = self.manager.register_provider(provider)
        self.assertEqual(result["status"], "SENSITIVE_METADATA_REJECTED")
        self.assertFalse(self.manager.provider_exists("unsafe"))
        nested = {
            "id": "unsafe_nested", "name": "Unsafe", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API",
            "metadata": {"credentials": {"token": "never-store-this"}},
        }
        nested_result = self.manager.register_provider(nested)
        self.assertEqual(nested_result["status"], "SENSITIVE_METADATA_REJECTED")
        self.assertFalse(self.manager.provider_exists("unsafe_nested"))

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
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        results = self.manager.discover_providers(capability="EDIT")
        self.assertEqual([item["id"] for item in results], ["public_image"])

    def test_discovery_enabled_only_excludes_unspecified_and_disabled(self):
        self.manager.catalog["providers"][0]["enabled"] = True
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
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
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
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
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
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
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        metadata = self.manager.get_provider_metadata("public_image")
        self.assertEqual(metadata["official_api_url"], "https://example.invalid")
        self.assertEqual(metadata["token_env"], "PUBLIC_IMAGE_TOKEN")
        self.assertNotIn("access_token", metadata)
        sanitized = ConnectionManager._sanitize_public_data({
            "access_token": "must-not-appear",
            "authorization_header": "bearer-secret",
            "client_secret": "client-secret",
            "nested": {
                "refresh_token": "also-secret",
                "session_cookie": "cookie-secret",
                "signing_key": "private-key",
                "region": "global",
            },
        })
        for secret_key in ("access_token", "authorization_header", "client_secret"):
            self.assertNotIn(secret_key, sanitized)
        for secret_value in ("refresh_token", "must-not-appear", "bearer-secret", "client-secret", "cookie-secret", "private-key"):
            self.assertNotIn(secret_value, repr(sanitized))
        metadata["name"] = "mutated"
        self.assertNotEqual(self.manager.get_provider("public_image").get("name"), "mutated")

    def test_catalog_status_is_not_exposed_as_live_connection_status(self):
        self.manager.catalog["providers"][0]["status"] = "CONNECTED"
        self.catalog_path.write_text(json.dumps(self.manager.catalog), encoding="utf-8")
        listed = self.manager.list_providers()[0]
        metadata = self.manager.get_provider_metadata("public_image")
        self.assertEqual(listed["catalog_status"], "CONNECTED")
        self.assertNotIn("status", listed)
        self.assertEqual(metadata["catalog_status"], "CONNECTED")
        self.assertNotIn("status", metadata)
        self.assertNotEqual(self.manager.get_state("public_image")["status"], "CONNECTED")

    def test_metadata_returns_none_for_unknown_provider(self):
        self.assertIsNone(self.manager.get_provider_metadata("not_registered"))

    def test_connectivity_probe_marks_connected_only_after_success(self):
        result = self.manager.verify_connectivity(
            "public_image", lambda provider, timeout_seconds: True
        )
        self.assertEqual(result["status"], "CONNECTED")
        self.assertTrue(result["verified"])
        self.assertEqual(self.manager.get_state("public_image")["status"], "CONNECTED")
        self.assertTrue(self.manager.get_state("public_image")["verified"])

    def test_connectivity_probe_requires_credentials_and_never_probes_without_them(self):
        called = []
        result = self.manager.verify_connectivity(
            "token_image", lambda *args, **kwargs: called.append(True), environ={}
        )
        self.assertEqual(result["status"], "CREDENTIALS_MISSING")
        self.assertEqual(called, [])

    def test_connectivity_probe_classifies_timeout_and_does_not_claim_connected(self):
        def timeout_probe(provider, timeout_seconds):
            raise TimeoutError("timeout")
        result = self.manager.verify_connectivity("public_image", timeout_probe)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertFalse(result["verified"])
        self.assertNotEqual(self.manager.get_state("public_image")["status"], "CONNECTED")

    def test_connectivity_probe_classifies_auth_and_network_failures_without_details(self):
        def auth_probe(provider, timeout_seconds):
            raise PermissionError("private credential details")
        result = self.manager.verify_connectivity("public_image", auth_probe)
        self.assertEqual(result["status"], "AUTHENTICATION_OR_PERMISSION_FAILED")
        self.assertNotIn("private credential details", repr(result))
        def network_probe(provider, timeout_seconds):
            raise ConnectionError("private endpoint details")
        result = self.manager.verify_connectivity("public_image", network_probe)
        self.assertEqual(result["status"], "NETWORK_ERROR")
        self.assertNotIn("private endpoint details", repr(result))

    def test_connectivity_probe_validates_timeout_and_disabled_provider(self):
        self.assertEqual(
            self.manager.verify_connectivity(
                "public_image", lambda *a, **k: True, timeout_seconds=0
            )["status"],
            "INVALID_TIMEOUT",
        )
        self.assertEqual(
            self.manager.verify_connectivity(
                "public_image", lambda *a, **k: True, timeout_seconds=float("nan")
            )["status"],
            "INVALID_TIMEOUT",
        )
        self.assertEqual(
            self.manager.verify_connectivity(
                "public_image", lambda *a, **k: True, timeout_seconds=float("inf")
            )["status"],
            "INVALID_TIMEOUT",
        )
        self.manager.set_provider_enabled("public_image", False)
        self.assertEqual(
            self.manager.verify_connectivity("public_image", lambda *a, **k: True)["status"],
            "DISABLED",
        )

    def test_endpoint_validation_requires_https_and_blocks_embedded_credentials(self):
        self.assertEqual(
            ConnectionManager.validate_endpoint("http://provider.example")["status"],
            "HTTPS_REQUIRED",
        )
        self.assertEqual(
            ConnectionManager.validate_endpoint("https://user:pass@provider.example")["status"],
            "EMBEDDED_CREDENTIALS_REJECTED",
        )
        self.assertEqual(
            ConnectionManager.validate_endpoint("https://provider.example/path?token=secret")["status"],
            "QUERY_PARAMETERS_REJECTED",
        )
        self.assertEqual(
            ConnectionManager.validate_endpoint("https://provider.example/path#fragment")["status"],
            "FRAGMENT_REJECTED",
        )
        self.assertTrue(ConnectionManager.validate_endpoint("https://provider.example")["valid"])

    def test_endpoint_validation_enforces_allowlist_and_local_http_policy(self):
        result = ConnectionManager.validate_endpoint(
            "https://other.example", allowed_hosts={"provider.example"}
        )
        self.assertEqual(result["status"], "HOST_NOT_ALLOWLISTED")
        self.assertEqual(
            ConnectionManager.validate_endpoint("https://169.254.169.254/latest/meta-data")["status"],
            "NON_PUBLIC_ADDRESS_REJECTED",
        )
        self.assertEqual(
            ConnectionManager.validate_endpoint("https://10.0.0.8")["status"],
            "NON_PUBLIC_ADDRESS_REJECTED",
        )
        self.assertTrue(
            ConnectionManager.validate_endpoint(
                "https://10.0.0.8", allowed_hosts={"10.0.0.8"}
            )["valid"]
        )
        self.assertEqual(
            ConnectionManager.validate_endpoint("http://127.0.0.1:8000")["status"],
            "HTTPS_REQUIRED",
        )
        local = ConnectionManager.validate_endpoint(
            "http://127.0.0.1:8000", allow_loopback_http=True
        )
        self.assertTrue(local["valid"])
        self.assertFalse(local["tls_verification_required"])
        self.assertEqual(
            ConnectionManager.validate_endpoint(
                "http://127.0.0.1:8000", allow_loopback_http="yes"
            )["status"],
            "INVALID_LOOPBACK_HTTP_POLICY",
        )

    def test_provider_model_configuration_is_persisted_without_credentials(self):
        result = self.manager.configure_provider("public_image", model="test-model-v1")
        self.assertEqual(result["status"], "CONFIGURED")
        reloaded = ConnectionManager(self.catalog_path, self.state_path)
        self.assertEqual(
            reloaded.get_state("public_image")["configuration"]["model"], "test-model-v1"
        )

    def test_provider_endpoint_configuration_rejects_unsafe_or_secret_urls(self):
        self.assertEqual(
            self.manager.configure_provider(
                "public_image", endpoint="http://provider.example/api"
            )["status"],
            "HTTPS_REQUIRED",
        )
        self.assertEqual(
            self.manager.configure_provider(
                "public_image", endpoint="https://provider.example/api?api_key=secret"
            )["status"],
            "QUERY_PARAMETERS_REJECTED",
        )
        self.assertEqual(
            self.manager.configure_provider(
                "public_image", endpoint="https://provider.example/api"
            )["status"],
            "HOST_ALLOWLIST_REQUIRED",
        )
        self.assertNotIn("configuration", self.manager.get_state("public_image"))


if __name__ == "__main__":
    unittest.main()
