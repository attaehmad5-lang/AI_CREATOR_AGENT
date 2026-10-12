import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager


class ProviderCatalogValidationTests(unittest.TestCase):
    def test_primary_and_master_provider_catalogs_are_synchronized(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        primary = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        master = json.loads((root / "provider_catalog_master.json").read_text(encoding="utf-8"))
        self.assertEqual(primary.get("version"), master.get("version"))
        self.assertEqual(primary.get("providers"), master.get("providers"))

    def test_schema_rejects_enabled_api_provider_without_allowlist(self):
        result = ConnectionManager.validate_catalog_schema({
            "version": "3.0",
            "providers": [{
                "id": "unsafe_api", "name": "Unsafe API",
                "category": "image", "auth_type": "NONE",
                "connection_type": "API", "enabled": True,
            }]
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("allowed_hosts is required" in error for error in result["errors"]))

    def test_enabled_api_providers_have_explicit_host_allowlists(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        for provider in catalog["providers"]:
            if provider.get("enabled") and str(provider.get("connection_type", "")).upper() == "API":
                hosts = provider.get("allowed_hosts")
                self.assertIsInstance(hosts, list, provider.get("id"))
                self.assertTrue(hosts, provider.get("id"))
                self.assertTrue(
                    all(isinstance(host, str) and host.strip() == host for host in hosts),
                    provider.get("id"),
                )

    def test_every_catalog_modality_has_a_callable_contract(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        required_methods = (
            "generate_image", "generate_video", "generate_speech", "generate_audio",
            "generate_music", "generate_text", "transcribe", "translate", "dub",
            "edit_image", "upscale_image", "upscale_video", "generate_lipsync", "edit_video",
        )
        adapter = type("CompleteTestAdapter", (), {
            method: (lambda self, *args, **kwargs: None) for method in required_methods
        })()
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            catalog_path = temp / "catalog.json"
            state_path = temp / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(json.dumps({
                "version": "1.0", "policy": "USER_CONTROLLED", "providers": {},
            }), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            for provider in catalog["providers"]:
                with self.subTest(provider=provider["id"], category=provider["category"]):
                    result = manager.validate_provider_contract(provider["id"], adapter)
                    self.assertTrue(result["valid"], result)

    def test_capability_identifiers_must_be_bounded_unique_and_normalized(self):
        base = {
            "version": "3.0",
            "providers": [{
                "id": "capability_test", "name": "Capability Test",
                "category": "image", "auth_type": "NONE",
                "connection_type": "API", "enabled": False,
            }],
        }
        for capabilities in (
            [""],
            ["Image-Generation"],
            ["image_generation", "image_generation"],
            ["image_generation", "IMAGE_GENERATION"],
            ["x" * 81],
            ["capability_" + str(index) for index in range(101)],
        ):
            with self.subTest(capabilities=capabilities):
                candidate = json.loads(json.dumps(base))
                candidate["providers"][0]["capabilities"] = capabilities
                result = ConnectionManager.validate_catalog_schema(candidate)
                self.assertFalse(result["valid"])
                self.assertTrue(any("capabilities must contain unique" in error for error in result["errors"]))

        valid = json.loads(json.dumps(base))
        valid["providers"][0]["capabilities"] = ["image_generation", "inpainting_v2"]
        self.assertTrue(ConnectionManager.validate_catalog_schema(valid)["valid"])

    def test_all_provider_catalog_json_files_pass_schema_validation(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalogs = []
        for path in sorted(root.rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                catalogs.append(path)
                result = ConnectionManager.validate_catalog_schema(data)
                self.assertTrue(result["valid"], f"{path.name}: {result['errors']}")
        self.assertTrue(catalogs, "No provider catalog JSON files were found")


if __name__ == "__main__":
    unittest.main()
