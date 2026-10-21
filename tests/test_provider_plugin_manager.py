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

    def test_unload_failure_preserves_loaded_plugin_without_error_message_leak(self):
        plugin = ImagePlugin(fail_close=True)
        self.plugins.register_factory("public_image", lambda provider: plugin)
        self.assertEqual(self.plugins.load("public_image")["status"], "LOADED")
        result = self.plugins.unload("public_image")
        self.assertEqual(result["status"], "UNLOAD_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("private plugin failure", repr(result))
        self.assertIs(self.connection_manager.get_loaded_provider("public_image"), plugin)

    def test_constructor_rejects_unsafe_allowlist(self):
        for ids in ("public_image", ["bad id"], ["public_image", "public_image"], ["x" * 101]):
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    ProviderPluginManager(self.connection_manager, approved_plugin_ids=ids)


class ImagePlugin:
    def __init__(self, *, fail_close=False):
        self.closed = False
        self.fail_close = fail_close

    def generate_image(self, prompt, output_path):
        return {"ok": True}

    def close(self):
        if self.fail_close:
            raise RuntimeError("private plugin failure")
        self.closed = True


if __name__ == "__main__":
    unittest.main()
