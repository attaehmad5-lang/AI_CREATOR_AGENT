"""Provider config migration, safe reload, and rollback tests."""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.provider_config_lifecycle import ProviderConfigLifecycleManager
import projects.provider_store.provider_config_lifecycle as lifecycle_module


def valid_config(config):
    return isinstance(config.get("schema_version"), str) and isinstance(config.get("providers"), dict)


def migrate_v1_to_v2(config):
    config["schema_version"] = "2"
    config.setdefault("providers", {})
    config["reload_mode"] = "atomic"
    return config


class ProviderConfigLifecycleManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = ProviderConfigLifecycleManager(valid_config)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_requires_explicit_path_and_validates_each_version(self):
        original = {"schema_version": "1", "providers": {"image": {}}}
        result = self.manager.migrate(
            original, target_version="2", migrations={"1": migrate_v1_to_v2}
        )
        self.assertTrue(result["migrated"])
        self.assertEqual(result["config"]["schema_version"], "2")
        missing = self.manager.migrate(
            original, target_version="3", migrations={"1": migrate_v1_to_v2}
        )
        self.assertEqual(missing["status"], "MIGRATION_PATH_MISSING")
        self.assertEqual(original["schema_version"], "1")

    def test_hot_reload_rejects_bad_candidate_and_keeps_last_good_config(self):
        good = {"schema_version": "1", "providers": {"image": {}}}
        self.assertEqual(self.manager.reload(good)["status"], "HOT_RELOAD_APPLIED")
        bad = {"schema_version": 2, "providers": []}
        rejected = self.manager.reload(bad)
        self.assertEqual(rejected["status"], "HOT_RELOAD_REJECTED")
        self.assertEqual(self.manager.active_config, good)
        self.assertEqual(self.manager.generation, 1)

    def test_rollback_restores_previous_known_good_generation(self):
        first = {"schema_version": "1", "providers": {"image": {}}}
        second = {"schema_version": "2", "providers": {"video": {}}}
        self.manager.reload(first)
        self.manager.reload(second)
        rolled = self.manager.rollback()
        self.assertEqual(rolled["status"], "CONFIG_ROLLED_BACK")
        self.assertEqual(self.manager.active_config, first)
        self.assertEqual(self.manager.generation, 3)

    def test_rejected_rollback_preserves_history_and_active_config(self):
        allow_old = {"value": True}

        def changing_validator(config):
            return (
                isinstance(config.get("schema_version"), str)
                and isinstance(config.get("providers"), dict)
                and (config.get("schema_version") != "1" or allow_old["value"])
            )

        manager = ProviderConfigLifecycleManager(changing_validator)
        first = {"schema_version": "1", "providers": {"image": {}}}
        second = {"schema_version": "2", "providers": {"video": {}}}
        self.assertTrue(manager.reload(first)["reloaded"])
        self.assertTrue(manager.reload(second)["reloaded"])

        allow_old["value"] = False
        rejected = manager.rollback()
        self.assertEqual(rejected["status"], "ROLLBACK_CONFIG_INVALID")
        self.assertEqual(manager.active_config, second)
        self.assertEqual(manager.generation, 2)

        allow_old["value"] = True
        retried = manager.rollback()
        self.assertEqual(retried["status"], "CONFIG_ROLLED_BACK")
        self.assertEqual(manager.active_config, first)
        self.assertEqual(manager.generation, 3)

    def test_file_migration_creates_backup_and_writes_atomically(self):
        path = self.root / "provider-config.json"
        original = {"schema_version": "1", "providers": {"image": {}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        result = self.manager.migrate_file(
            path, target_version="2", migrations={"1": migrate_v1_to_v2}
        )
        self.assertEqual(result["status"], "CONFIG_MIGRATION_APPLIED")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], "2")
        self.assertEqual(json.loads(Path(str(path) + ".bak").read_text(encoding="utf-8"))["schema_version"], "1")

    def test_file_migration_runs_inside_interprocess_lock(self):
        path = self.root / "provider-config.json"
        path.write_text(
            json.dumps({"schema_version": "1", "providers": {"image": {}}}),
            encoding="utf-8",
        )
        with patch.object(lifecycle_module, "interprocess_file_lock") as lock_factory:
            lock_context = lock_factory.return_value
            with patch.object(
                self.manager,
                "_migrate_file_locked",
                return_value={"status": "LOCKED"},
            ) as migrate_locked:
                result = self.manager.migrate_file(
                    path, target_version="2", migrations={"1": migrate_v1_to_v2}
                )
        self.assertEqual(result["status"], "LOCKED")
        lock_factory.assert_called_once_with(path.with_name(path.name + ".lock"))
        lock_context.__enter__.assert_called_once_with()
        migrate_locked.assert_called_once()

    def test_invalid_config_file_is_not_overwritten(self):
        path = self.root / "bad.json"
        path.write_text("{broken", encoding="utf-8")
        result = self.manager.migrate_file(path, target_version="2", migrations={})
        self.assertEqual(result["status"], "INVALID_CONFIG_FILE")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
