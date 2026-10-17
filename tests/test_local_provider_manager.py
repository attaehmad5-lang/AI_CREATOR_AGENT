import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.local_provider_manager import LocalProviderManager


class LocalProviderManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "local-providers.json"
        self.manager = LocalProviderManager(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def register(self, provider_id="ollama_local", endpoint="http://127.0.0.1:11434/v1"):
        return self.manager.register(
            provider_id=provider_id,
            endpoint=endpoint,
            model="qwen2.5:7b",
            capabilities=["text_generation"],
        )

    def test_loopback_provider_registers_disabled_until_model_probe_passes(self):
        result = self.register()
        self.assertEqual(result["status"], "REGISTERED_DISABLED")
        record = self.manager.list_providers()[0]
        self.assertFalse(record["enabled"])
        self.assertFalse(record["verified"])
        blocked = self.manager.enable("ollama_local", probe=None)
        self.assertEqual(blocked["status"], "LIVE_PROBE_REQUIRED")
        enabled = self.manager.enable(
            "ollama_local",
            probe=lambda **kwargs: {"ok": True, "model": "qwen2.5:7b"},
        )
        self.assertEqual(enabled["status"], "LOCAL_PROVIDER_ENABLED")
        self.assertTrue(self.manager.list_providers()[0]["verified"])

    def test_model_mismatch_and_probe_errors_do_not_enable_provider(self):
        self.register()
        mismatch = self.manager.enable(
            "ollama_local",
            probe=lambda **kwargs: {"ok": True, "model": "different-model"},
        )
        self.assertEqual(mismatch["status"], "LOCAL_PROVIDER_MODEL_UNVERIFIED")
        failed = self.manager.enable(
            "ollama_local",
            probe=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("private error")),
        )
        self.assertEqual(failed["status"], "LOCAL_PROVIDER_PROBE_FAILED")
        record = self.manager.list_providers()[0]
        self.assertFalse(record["enabled"])
        self.assertFalse(record["verified"])

    def test_non_loopback_host_requires_exact_allowlist_and_https(self):
        result = self.manager.register(
            provider_id="local_remote",
            endpoint="http://192.168.1.20:11434/v1",
            model="qwen2.5:7b",
            capabilities=["text_generation"],
        )
        self.assertEqual(result["status"], "LOCAL_HOST_NOT_ALLOWLISTED")
        insecure = self.manager.register(
            provider_id="local_remote",
            endpoint="http://models.internal/v1",
            model="qwen2.5:7b",
            capabilities=["text_generation"],
            allowed_hosts=["models.internal"],
        )
        self.assertEqual(insecure["status"], "REMOTE_LOCAL_ENDPOINT_REQUIRES_HTTPS")
        secure = self.manager.register(
            provider_id="local_remote",
            endpoint="https://models.internal/v1",
            model="qwen2.5:7b",
            capabilities=["text_generation"],
            allowed_hosts=["models.internal"],
        )
        self.assertTrue(secure["registered"])

    def test_endpoint_credentials_queries_and_unapproved_hosts_are_rejected(self):
        endpoints = [
            "http://user:password@127.0.0.1:11434/v1",
            "http://127.0.0.1:11434/v1?token=secret",
            "https://evil.example/v1",
            "ftp://localhost/model",
            "http://localhost:99999/v1",
        ]
        for index, endpoint in enumerate(endpoints):
            with self.subTest(index=index):
                result = self.manager.register(
                    provider_id=f"local_bad_{index}",
                    endpoint=endpoint,
                    model="qwen2.5:7b",
                    capabilities=["text_generation"],
                )
                self.assertFalse(result["registered"])

    def test_invalid_capabilities_and_secret_like_model_names_are_rejected(self):
        for index, capabilities in enumerate(([], ["unknown"], ["text_generation", "text_generation"], [None])):
            with self.subTest(index=index):
                result = self.manager.register(
                    provider_id=f"local_caps_{index}",
                    endpoint="http://localhost:11434/v1",
                    model="qwen2.5:7b",
                    capabilities=capabilities,
                )
                self.assertFalse(result["registered"])
        secret_model = self.manager.register(
            provider_id="local_secret",
            endpoint="http://localhost:11434/v1",
            model="api_key=secret",
            capabilities=["text_generation"],
        )
        self.assertEqual(secret_model["status"], "INVALID_MODEL_ID")

    def test_duplicate_registration_and_disable_are_safe(self):
        self.assertTrue(self.register()["registered"])
        self.assertEqual(self.register()["status"], "PROVIDER_ALREADY_REGISTERED")
        self.manager.enable(
            "ollama_local",
            probe=lambda **kwargs: {"ok": True, "model": "qwen2.5:7b"},
        )
        disabled = self.manager.disable("ollama_local")
        self.assertEqual(disabled["status"], "LOCAL_PROVIDER_DISABLED")
        self.assertFalse(self.manager.list_providers()[0]["enabled"])
        self.assertFalse(self.manager.list_providers()[0]["verified"])

    def test_registry_corruption_fails_closed(self):
        self.path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            LocalProviderManager(self.path)

    def test_local_provider_registry_never_stores_credentials(self):
        self.register()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(stored["providers"]["ollama_local"]), {
            "endpoint", "model", "capabilities", "allowed_hosts", "enabled", "verified"
        })
        self.assertNotIn("password", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
