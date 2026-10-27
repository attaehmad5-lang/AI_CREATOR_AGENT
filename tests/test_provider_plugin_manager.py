"""Allowlisted provider plugin lifecycle tests."""
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_plugin_manager import ProviderPluginManager
from concurrent.futures import ThreadPoolExecutor


class ProviderPluginManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = self.root / "catalog.json"
        self.state = self.root / "state.json"
        self.catalog.write_text(json.dumps({
            "providers": [{
                "id": "public_image",
                "name": "Public Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "LOCAL",
                "enabled": True,
            }]
        }), encoding="utf-8")
        self.state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connection_manager = ConnectionManager(self.catalog, self.state)
        self.plugins = ProviderPluginManager(
            self.connection_manager, approved_plugin_ids={"public_image"}
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_factory_registration_is_allowlisted_and_does_not_execute_plugin(self):
        calls = []

        def factory(provider):
            calls.append(provider["id"])
            return ImagePlugin()

        denied = self.plugins.register_factory("unknown_provider", factory)
        self.assertEqual(denied["status"], "PLUGIN_NOT_APPROVED")
        registered = self.plugins.register_factory("public_image", factory)
        self.assertEqual(registered["status"], "FACTORY_REGISTERED")
        self.assertEqual(calls, [])
        self.assertEqual(
            self.plugins.register_factory("public_image", factory)["status"],
            "FACTORY_ALREADY_REGISTERED",
        )
        self.assertEqual(self.plugins.register_factory("public_image", "module:class")["status"], "INVALID_FACTORY")

    def test_concurrent_factory_registration_is_atomic_under_contention(self):
        factory = lambda provider: ImagePlugin()
        workers = 32
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda _: self.plugins.register_factory("public_image", factory),
                range(workers),
            ))

        statuses = [result["status"] for result in results]
        self.assertEqual(statuses.count("FACTORY_REGISTERED"), 1)
        self.assertEqual(statuses.count("FACTORY_ALREADY_REGISTERED"), workers - 1)
        self.assertEqual(self.plugins.status()["registered_plugin_ids"], ["public_image"])

    def test_load_unload_and_unregister_follow_validated_lifecycle(self):
        plugin = ImagePlugin()
        self.assertEqual(self.plugins.register_factory("public_image", lambda provider: plugin)["status"], "FACTORY_REGISTERED")
        loaded = self.plugins.load("public_image")
        self.assertEqual(loaded["status"], "LOADED")
        self.assertEqual(self.plugins.unregister_factory("public_image")["status"], "PLUGIN_ACTIVE")
        self.assertEqual(self.plugins.status()["loaded_plugin_ids"], ["public_image"])

        unloaded = self.plugins.unload("public_image")
        self.assertEqual(unloaded["status"], "UNLOADED")
        self.assertTrue(plugin.closed)
        self.assertEqual(self.plugins.unregister_factory("public_image")["status"], "FACTORY_UNREGISTERED")
        self.assertEqual(self.plugins.status()["loaded_plugin_ids"], [])

    def test_factory_mutating_provider_metadata_cannot_change_persisted_catalog(self):
        def mutating_factory(provider):
            provider["name"] = "MUTATED BY PLUGIN"
            provider["enabled"] = False
            provider["capabilities"] = ["admin_override"]
            return ImagePlugin()

        self.assertEqual(
            self.plugins.register_factory("public_image", mutating_factory)["status"],
            "FACTORY_REGISTERED",
        )
        result = self.plugins.load("public_image")

        self.assertEqual(result["status"], "LOADED")
        persisted = self.connection_manager.get_provider("public_image")
        self.assertEqual(persisted["name"], "Public Image")
        self.assertTrue(persisted["enabled"])
        self.assertNotIn("capabilities", persisted)

    def test_disabled_provider_is_rejected_before_factory_execution(self):
        disabled_catalog = self.root / "disabled-catalog.json"
        disabled_catalog.write_text(json.dumps({
            "providers": [{
                "id": "public_image",
                "name": "Public Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "LOCAL",
                "enabled": False,
            }]
        }), encoding="utf-8")
        disabled_manager = ConnectionManager(disabled_catalog, self.state)
        plugin_manager = ProviderPluginManager(
            disabled_manager, approved_plugin_ids={"public_image"}
        )
        calls = []
        plugin_manager.register_factory(
            "public_image",
            lambda provider: calls.append(provider["id"]) or ImagePlugin(),
        )

        result = plugin_manager.load("public_image")

        self.assertEqual(result["status"], "DISABLED")
        self.assertFalse(result["loaded"])
        self.assertEqual(calls, [])
        self.assertIsNone(disabled_manager.get_loaded_provider("public_image"))

    def test_factory_exception_is_sanitized_and_runtime_remains_empty(self):
        def unsafe_factory(provider):
            raise RuntimeError("secret endpoint https://private.example/token=abc")

        self.plugins.register_factory("public_image", unsafe_factory)
        result = self.plugins.load("public_image")

        self.assertEqual(result["status"], "LOAD_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("private.example", repr(result))
        self.assertNotIn("token=abc", repr(result))
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_status_waits_for_inflight_load_and_reports_consistent_snapshot(self):
        entered_factory = Event()
        release_factory = Event()
        plugin = ImagePlugin()

        def slow_factory(provider):
            entered_factory.set()
            if not release_factory.wait(timeout=5):
                raise RuntimeError("test factory timed out")
            return plugin

        self.plugins.register_factory("public_image", slow_factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            load_future = pool.submit(self.plugins.load, "public_image")
            self.assertTrue(entered_factory.wait(timeout=2))
            status_future = pool.submit(self.plugins.status)
            self.assertFalse(status_future.done())
            release_factory.set()
            self.assertEqual(load_future.result(timeout=5)["status"], "LOADED")
            status = status_future.result(timeout=5)

        self.assertEqual(status["registered_plugin_ids"], ["public_image"])
        self.assertEqual(status["loaded_plugin_ids"], ["public_image"])

    def test_interface_version_negotiation_rejects_unsupported_versions(self):
        factory = lambda provider: ImagePlugin()
        denied = self.plugins.register_factory("public_image", factory, interface_version="2.0")
        self.assertEqual(denied["status"], "UNSUPPORTED_INTERFACE_VERSION")
        self.assertFalse(denied["registered"])
        self.assertEqual(self.plugins.status()["registered_plugin_ids"], [])

        accepted = self.plugins.register_factory("public_image", factory, interface_version="1.0")
        self.assertEqual(accepted["status"], "FACTORY_REGISTERED")
        self.assertEqual(accepted["interface_version"], "1.0")
        status = self.plugins.status()
        self.assertEqual(status["supported_interface_versions"], ["1.0"])
        self.assertEqual(status["registered_interface_versions"], {"public_image": "1.0"})

    def test_runtime_interface_version_mismatch_unloads_incompatible_plugin(self):
        plugin = ImagePlugin(interface_version="2.0")
        self.plugins.register_factory("public_image", lambda provider: plugin, interface_version="1.0")

        result = self.plugins.load("public_image")

        self.assertEqual(result["status"], "INTERFACE_VERSION_MISMATCH")
        self.assertEqual(result["declared_interface_version"], "1.0")
        self.assertEqual(result["actual_interface_version"], "2.0")
        self.assertFalse(result["loaded"])
        self.assertTrue(plugin.closed)
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_runtime_interface_version_must_be_declared(self):
        plugin = ImagePlugin()
        del plugin.interface_version
        self.plugins.register_factory("public_image", lambda provider: plugin, interface_version="1.0")

        result = self.plugins.load("public_image")

        self.assertEqual(result["status"], "INTERFACE_VERSION_MISMATCH")
        self.assertIsNone(result["actual_interface_version"])
        self.assertTrue(plugin.closed)
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_interface_version_validation_rejects_malformed_versions(self):
        for version in ("", "v1", "1", "1.0.0", "01.0", "1.1000", "1.0\\n"):
            with self.subTest(version=version):
                result = self.plugins.register_factory(
                    "public_image", lambda provider: ImagePlugin(), interface_version=version
                )
                self.assertEqual(result["status"], "INVALID_INTERFACE_VERSION")
        for versions in (("1.0", "1.0"), ("v1",), (), ("1.0.0",)):
            with self.subTest(versions=versions):
                with self.assertRaises(ValueError):
                    ProviderPluginManager(
                        self.connection_manager,
                        approved_plugin_ids={"public_image"},
                        supported_interface_versions=versions,
                    )

    def test_invalid_plugin_contract_never_enters_runtime(self):
        self.plugins.register_factory("public_image", lambda provider: object())
        result = self.plugins.load("public_image")
        self.assertEqual(result["status"], "CONTRACT_VIOLATION")
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_unregister_waits_for_inflight_load_then_refuses_active_factory_removal(self):
        entered_factory = Event()
        release_factory = Event()
        plugin = ImagePlugin()

        def slow_factory(provider):
            entered_factory.set()
            if not release_factory.wait(timeout=5):
                raise RuntimeError("test factory timed out")
            return plugin

        self.plugins.register_factory("public_image", slow_factory)
        with ThreadPoolExecutor(max_workers=2) as pool:
            load_future = pool.submit(self.plugins.load, "public_image")
            self.assertTrue(entered_factory.wait(timeout=2))
            unregister_future = pool.submit(self.plugins.unregister_factory, "public_image")
            self.assertFalse(unregister_future.done())
            release_factory.set()
            loaded = load_future.result(timeout=5)
            unregister = unregister_future.result(timeout=5)

        self.assertEqual(loaded["status"], "LOADED")
        self.assertEqual(unregister["status"], "PLUGIN_ACTIVE")
        self.assertIs(self.connection_manager.get_loaded_provider("public_image"), plugin)

    def test_contract_violation_closes_partially_initialized_instance(self):
        plugin = PartialImagePlugin()
        self.plugins.register_factory("public_image", lambda provider: plugin)
        result = self.plugins.load("public_image")
        self.assertEqual(result["status"], "CONTRACT_VIOLATION")
        self.assertFalse(result["loaded"])
        self.assertTrue(plugin.closed)
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_contract_violation_reports_cleanup_error_type_without_message(self):
        plugin = PartialImagePlugin(fail_close=True)
        self.plugins.register_factory("public_image", lambda provider: plugin)
        result = self.plugins.load("public_image")
        self.assertEqual(result["status"], "CONTRACT_VIOLATION")
        self.assertEqual(result["cleanup_error_type"], "RuntimeError")
        self.assertNotIn("private cleanup failure", repr(result))
        self.assertIsNone(self.connection_manager.get_loaded_provider("public_image"))

    def test_unload_failure_preserves_loaded_plugin_without_error_message_leak(self):
        plugin = ImagePlugin(fail_close=True)
        self.plugins.register_factory("public_image", lambda provider: plugin)
        self.assertEqual(self.plugins.load("public_image")["status"], "LOADED")
        result = self.plugins.unload("public_image")
        self.assertEqual(result["status"], "UNLOAD_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("private plugin failure", repr(result))
        self.assertIs(self.connection_manager.get_loaded_provider("public_image"), plugin)

    def test_status_is_consistent_and_never_exposes_factory_or_instance(self):
        plugin = ImagePlugin()
        secret_marker = "DO_NOT_EXPOSE_FACTORY_OR_INSTANCE"
        self.plugins.register_factory(
            "public_image",
            lambda provider: (setattr(plugin, "secret_marker", secret_marker) or plugin),
        )
        self.assertEqual(self.plugins.load("public_image")["status"], "LOADED")

        status = self.plugins.status()
        rendered = repr(status)

        self.assertEqual(status["registered_plugin_ids"], ["public_image"])
        self.assertEqual(status["loaded_plugin_ids"], ["public_image"])
        self.assertNotIn(secret_marker, rendered)
        self.assertNotIn("factory", status)
        self.assertNotIn("instance", status)
        self.assertFalse(status["arbitrary_module_imports_allowed"])
        self.assertFalse(status["process_isolation_enabled"])

    def test_constructor_rejects_unsafe_allowlist(self):
        for ids in ("public_image", ["bad id"], ["public_image", "public_image"], ["x" * 101]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    ProviderPluginManager(self.connection_manager, approved_plugin_ids=ids)


class PartialImagePlugin:
    def __init__(self, *, fail_close=False):
        self.closed = False
        self.fail_close = fail_close

    def close(self):
        if self.fail_close:
            raise RuntimeError("private cleanup failure")
        self.closed = True


class ImagePlugin:
    def __init__(self, *, fail_close=False, interface_version="1.0"):
        self.closed = False
        self.fail_close = fail_close
        self.interface_version = interface_version

    def generate_image(self, prompt, output_path):
        return {"ok": True}

    def close(self):
        if self.fail_close:
            raise RuntimeError("private plugin failure")
        self.closed = True


if __name__ == "__main__":
    unittest.main()
