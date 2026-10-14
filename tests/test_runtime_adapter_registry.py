import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.runtime_adapter_registry import (
    BUILTIN_PROVIDER_FACTORIES,
    RuntimeProviderRegistry,
)


class _ImageAdapter:
    def generate_image(self, prompt, output_path):
        return {"status": "COMPLETED", "output": output_path}


class _BadAdapter:
    def generate_video(self, prompt, output_path):
        return {"status": "COMPLETED", "output": output_path}


def _provider(provider_id, *, enabled=True, category="image", extra=None):
    item = {
        "id": provider_id,
        "name": provider_id.replace("_", " ").title(),
        "category": category,
        "auth_type": "NONE",
        "connection_type": "API",
        "enabled": enabled,
        "subscription_required": False,
        "allowed_hosts": ["api.example.com"],
        "official_api_url": "https://api.example.com/docs",
        "capabilities": ["image_generation"] if category == "image" else ["video_generation"],
    }
    if extra:
        item.update(extra)
    return item


class RuntimeProviderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"
        self.catalog_path.write_text(json.dumps({
            "version": "3.0",
            "providers": [
                _provider("alpha"),
                _provider("beta"),
                _provider("disabled", enabled=False),
                _provider("video", category="video"),
            ],
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}}), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog_path, self.state_path)
        self.registry = RuntimeProviderRegistry(
            self.manager,
            factories={"alpha": lambda _: _ImageAdapter(), "video": lambda _: _BadAdapter()},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_builtin_factories_are_explicitly_allowlisted(self):
        self.assertEqual(set(BUILTIN_PROVIDER_FACTORIES), {"huggingface", "pollinations"})
        self.assertTrue(all(callable(factory) for factory in BUILTIN_PROVIDER_FACTORIES.values()))

    def test_constructor_rejects_non_mapping_factories(self):
        with self.assertRaises(ValueError):
            RuntimeProviderRegistry(self.manager, factories=[])

    def test_constructor_rejects_invalid_factory_id(self):
        with self.assertRaises(ValueError):
            RuntimeProviderRegistry(self.manager, factories={"bad id": lambda _: _ImageAdapter()})

    def test_constructor_rejects_non_callable_factory(self):
        with self.assertRaises(ValueError):
            RuntimeProviderRegistry(self.manager, factories={"alpha": "module.Class"})

    def test_registered_ids_are_sorted_and_deterministic(self):
        self.assertEqual(self.registry.registered_ids(), ["alpha", "video"])

    def test_load_unknown_provider_returns_not_found(self):
        result = self.registry.load("missing")
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertFalse(result["loaded"])

    def test_catalog_provider_without_factory_fails_closed(self):
        result = self.registry.load("beta")
        self.assertEqual(result["status"], "ADAPTER_NOT_REGISTERED")
        self.assertFalse(result["loaded"])

    def test_disabled_provider_cannot_be_loaded_even_with_factory(self):
        self.registry.register_factory("disabled", lambda _: _ImageAdapter())
        result = self.registry.load("disabled")
        self.assertEqual(result["status"], "DISABLED")
        self.assertFalse(result["loaded"])

    def test_valid_image_adapter_loads_through_connection_manager(self):
        result = self.registry.load("alpha")
        self.assertEqual(result["status"], "LOADED")
        self.assertIsInstance(self.registry.get("alpha"), _ImageAdapter)

    def test_duplicate_load_is_reported_without_replacing_instance(self):
        first = self.registry.load("alpha")
        instance = self.registry.get("alpha")
        second = self.registry.load("alpha")
        self.assertEqual(first["status"], "LOADED")
        self.assertEqual(second["status"], "ALREADY_LOADED")
        self.assertIs(self.registry.get("alpha"), instance)

    def test_wrong_modality_contract_is_rejected(self):
        result = self.registry.load("video")
        self.assertEqual(result["status"], "CONTRACT_VIOLATION")
        self.assertFalse(result["loaded"])
        self.assertIsNone(self.registry.get("video"))

    def test_factory_exception_is_safely_reported(self):
        def broken(_):
            raise RuntimeError("private token must not appear")
        self.registry.register_factory("beta", broken)
        result = self.registry.load("beta")
        self.assertEqual(result["status"], "LOAD_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("private token", repr(result))

    def test_factory_returning_none_fails_closed(self):
        self.registry.register_factory("beta", lambda _: None)
        result = self.registry.load("beta")
        self.assertEqual(result["status"], "LOAD_FAILED")
        self.assertFalse(result["loaded"])

    def test_register_factory_rejects_invalid_id(self):
        result = self.registry.register_factory("../alpha", lambda _: _ImageAdapter())
        self.assertEqual(result["status"], "INVALID_PROVIDER_ID")
        self.assertFalse(result["registered"])

    def test_register_factory_rejects_non_callable(self):
        result = self.registry.register_factory("beta", "not-a-factory")
        self.assertEqual(result["status"], "INVALID_FACTORY")
        self.assertFalse(result["registered"])

    def test_register_factory_requires_existing_catalog_provider(self):
        result = self.registry.register_factory("missing", lambda _: _ImageAdapter())
        self.assertEqual(result["status"], "PROVIDER_NOT_FOUND")
        self.assertFalse(result["registered"])

    def test_duplicate_factory_requires_explicit_replace(self):
        result = self.registry.register_factory("alpha", lambda _: _ImageAdapter())
        self.assertEqual(result["status"], "FACTORY_ALREADY_REGISTERED")
        self.assertFalse(result["registered"])

    def test_factory_can_be_replaced_before_provider_is_loaded(self):
        replacement = lambda _: _ImageAdapter()
        result = self.registry.register_factory("alpha", replacement, replace=True)
        self.assertEqual(result["status"], "FACTORY_REGISTERED")
        self.assertTrue(result["registered"])
        self.assertIs(self.registry._factories["alpha"], replacement)

    def test_factory_cannot_be_replaced_while_provider_is_loaded(self):
        self.assertEqual(self.registry.load("alpha")["status"], "LOADED")
        result = self.registry.register_factory("alpha", lambda _: _ImageAdapter(), replace=True)
        self.assertEqual(result["status"], "PROVIDER_ALREADY_LOADED")
        self.assertFalse(result["registered"])

    def test_unregister_unknown_factory_is_reported(self):
        result = self.registry.unregister_factory("missing")
        self.assertEqual(result["status"], "FACTORY_NOT_REGISTERED")

    def test_factory_cannot_be_unregistered_while_provider_is_loaded(self):
        self.registry.load("alpha")
        result = self.registry.unregister_factory("alpha")
        self.assertEqual(result["status"], "PROVIDER_ALREADY_LOADED")
        self.assertFalse(result["unregistered"])

    def test_unregister_factory_removes_only_registry_mapping(self):
        result = self.registry.unregister_factory("alpha")
        self.assertEqual(result["status"], "FACTORY_UNREGISTERED")
        self.assertNotIn("alpha", self.registry.registered_ids())
        self.assertIsNone(self.registry.get("alpha"))

    def test_unload_delegates_cleanup_to_connection_manager(self):
        self.registry.load("alpha")
        result = self.registry.unload("alpha")
        self.assertEqual(result["status"], "UNLOADED")
        self.assertIsNone(self.registry.get("alpha"))

    def test_load_all_enabled_uses_stable_order_and_reports_missing_adapters(self):
        result = self.registry.load_all_enabled()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["loaded"], ["alpha"])
        by_id = {item["provider"]: item["status"] for item in result["results"]}
        self.assertEqual(list(by_id), sorted(by_id))
        self.assertEqual(by_id["alpha"], "LOADED")
        self.assertEqual(by_id["beta"], "ADAPTER_NOT_REGISTERED")
        self.assertEqual(by_id["video"], "CONTRACT_VIOLATION")
        self.assertNotIn("disabled", by_id)

    def test_status_distinguishes_catalog_providers_from_registered_adapters(self):
        status = self.registry.status()
        self.assertEqual(status["catalog_provider_count"], 4)
        self.assertEqual(status["enabled_provider_count"], 3)
        self.assertEqual(status["registered_adapter_count"], 2)
        self.assertEqual(status["enabled_without_adapter"], ["beta"])
        self.assertEqual(status["status"], "READY")

    def test_catalog_metadata_cannot_direct_import_untrusted_module(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["providers"][0]["module"] = "os.system"
        catalog["providers"][0]["class"] = "system"
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog_path, self.state_path)
        registry = RuntimeProviderRegistry(self.manager, factories={"alpha": lambda _: _ImageAdapter()})
        result = registry.load("alpha")
        self.assertEqual(result["status"], "LOADED")
        self.assertIsInstance(registry.get("alpha"), _ImageAdapter)

    def test_registry_reports_no_adapters_loaded_when_all_factories_missing(self):
        registry = RuntimeProviderRegistry(self.manager, factories={})
        result = registry.load_all_enabled()
        self.assertEqual(result["status"], "NO_ADAPTERS_LOADED")
        self.assertEqual(result["loaded"], [])
        self.assertTrue(all(item["status"] == "ADAPTER_NOT_REGISTERED" for item in result["results"]))

    def test_invalid_provider_id_load_does_not_call_manager_loader(self):
        result = self.registry.load("../alpha")
        self.assertEqual(result["status"], "INVALID_PROVIDER_ID")
        self.assertFalse(result["loaded"])


if __name__ == "__main__":
    unittest.main()
