import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.catalog_migration_manager import ProviderCatalogMigrationManager
from projects.provider_store.connection_manager import ConnectionManager


class ProviderCatalogMigrationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "provider-catalog.json"
        self.backup = self.root / "provider-catalog.v2.bak"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def v2_catalog():
        return {
            "store": "AI_CREATOR_AGENT_PROVIDER_STORE",
            "version": "2.0",
            "status": "READY",
            "subscription_policy": "USER_CONTROLLED",
            "providers": [
                {
                    "id": "documented_api",
                    "name": "Documented API",
                    "category": "image",
                    "status": "CONNECTED",
                    "auth_type": "API_KEY",
                    "connection_type": "API",
                    "subscription_required": True,
                    "enabled": True,
                    "official_api_url": "https://api.example.com/docs",
                    "allowed_hosts": ["api.example.com"],
                    "capabilities": ["image_generation"],
                },
                {
                    "id": "legacy_unknown",
                    "name": "Legacy Unknown",
                    "category": "image",
                    "status": "CONNECTED",
                    "auth_type": "API_KEY",
                    "connection_type": "API",
                    "official_api_url": "https://legacy.example.com/docs",
                },
            ],
        }

    def test_migration_is_atomic_backed_up_valid_and_fail_closed(self):
        original_bytes = json.dumps(self.v2_catalog(), indent=2).encode("utf-8")
        self.target.write_bytes(original_bytes)
        result = ProviderCatalogMigrationManager.migrate_file(
            self.target, backup_path=self.backup
        )
        self.assertEqual(result["status"], "MIGRATED")
        self.assertTrue(result["migrated"])
        self.assertEqual(self.backup.read_bytes(), original_bytes)
        migrated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], "3.0")
        self.assertTrue(ConnectionManager.validate_catalog_schema(migrated)["valid"])
        providers = {item["id"]: item for item in migrated["providers"]}
        self.assertTrue(providers["documented_api"]["enabled"])
        self.assertFalse(providers["legacy_unknown"]["enabled"])
        self.assertTrue(providers["legacy_unknown"]["subscription_required"])
        self.assertEqual(providers["legacy_unknown"]["status"], "LOCKED")
        again = ProviderCatalogMigrationManager.migrate_file(
            self.target, backup_path=self.backup
        )
        self.assertEqual(again["status"], "ALREADY_CURRENT")
        self.assertFalse(again["migrated"])

    def test_legacy_connected_label_never_enables_provider_by_itself(self):
        catalog = self.v2_catalog()
        catalog["providers"] = [catalog["providers"][1]]
        self.target.write_text(json.dumps(catalog), encoding="utf-8")
        result = ProviderCatalogMigrationManager.migrate_file(self.target, backup_path=self.backup)
        self.assertEqual(result["status"], "MIGRATED")
        migrated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertFalse(migrated["providers"][0]["enabled"])
        self.assertEqual(migrated["providers"][0]["status"], "LOCKED")

    def test_unknown_version_and_invalid_source_are_not_modified(self):
        for catalog, expected in (
            ({**self.v2_catalog(), "version": "1.0"}, "UNSUPPORTED_SOURCE_VERSION"),
            ({**self.v2_catalog(), "providers": "not-a-list"}, "INVALID_SOURCE_CATALOG"),
        ):
            with self.subTest(expected=expected):
                before = json.dumps(catalog).encode("utf-8")
                self.target.write_bytes(before)
                result = ProviderCatalogMigrationManager.migrate_file(self.target, backup_path=self.backup)
                self.assertEqual(result["status"], expected)
                self.assertFalse(result["migrated"])
                self.assertEqual(self.target.read_bytes(), before)
                self.assertFalse(self.backup.exists())

    def test_existing_conflicting_backup_prevents_overwrite(self):
        original = self.v2_catalog()
        self.target.write_text(json.dumps(original), encoding="utf-8")
        self.backup.write_text("do-not-overwrite", encoding="utf-8")
        before = self.target.read_bytes()
        result = ProviderCatalogMigrationManager.migrate_file(self.target, backup_path=self.backup)
        self.assertEqual(result["status"], "BACKUP_CONFLICT")
        self.assertFalse(result["migrated"])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.backup.read_text(encoding="utf-8"), "do-not-overwrite")

    def test_duplicate_provider_ids_fail_without_writing(self):
        catalog = self.v2_catalog()
        catalog["providers"].append(dict(catalog["providers"][0]))
        before = json.dumps(catalog).encode("utf-8")
        self.target.write_bytes(before)
        result = ProviderCatalogMigrationManager.migrate_file(self.target, backup_path=self.backup)
        self.assertEqual(result["status"], "INVALID_SOURCE_CATALOG")
        self.assertFalse(result["migrated"])
        self.assertEqual(self.target.read_bytes(), before)

    def test_migrates_archival_v2_catalog_copy_without_touching_repository_backup(self):
        repository_backup = (
            Path(__file__).resolve().parents[1]
            / "projects" / "provider_store" / "provider_catalog_backup_v2.json"
        )
        if not repository_backup.is_file():
            self.skipTest("archival v2 catalog is unavailable")
        self.target.write_bytes(repository_backup.read_bytes())
        result = ProviderCatalogMigrationManager.migrate_file(self.target, backup_path=self.backup)
        self.assertEqual(result["status"], "MIGRATED", result)
        migrated = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertTrue(ConnectionManager.validate_catalog_schema(migrated)["valid"])
        self.assertEqual(self.backup.read_bytes(), repository_backup.read_bytes())


if __name__ == "__main__":
    unittest.main()
