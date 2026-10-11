import json
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager


class ProviderCatalogValidationTests(unittest.TestCase):
    def test_all_provider_catalog_json_files_pass_schema_validation(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalogs = []
        for path in sorted(root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                catalogs.append(path)
                result = ConnectionManager.validate_catalog_schema(data)
                self.assertTrue(result["valid"], f"{path.name}: {result['errors']}")
        self.assertTrue(catalogs, "No provider catalog JSON files were found")


if __name__ == "__main__":
    unittest.main()
