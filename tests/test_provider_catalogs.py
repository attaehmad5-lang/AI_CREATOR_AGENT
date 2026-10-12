import json
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
