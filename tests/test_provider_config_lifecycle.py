"""Provider config migration, safe reload, and rollback tests."""
import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.provider_config_lifecycle import ProviderConfigLifecycleManager


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

    def test_reload_validator_cannot_mutate_caller_or_active_config(self):
        def mutating_validator(config):
            config["validator_side_effect"] = True
            return True

        manager = ProviderConfigLifecycleManager(mutating_validator)
        candidate = {"schema_version": "1", "providers": {}}
        result = manager.reload(candidate)

        self.assertEqual(result["status"], "HOT_RELOAD_APPLIED")
        self.assertEqual(candidate, {"schema_version": "1", "providers": {}})
        self.assertEqual(manager.active_config, candidate)

    def test_reload_validator_exception_fails_closed_without_leaking_message(self):
        def raising_validator(config):
            raise RuntimeError("private validator detail")

        manager = ProviderConfigLifecycleManager(raising_validator)
        result = manager.reload({"schema_version": "1", "providers": {}})

        self.assertEqual(result["status"], "HOT_RELOAD_REJECTED")
        self.assertEqual(result["reason"], "CONFIG_VALIDATION_FAILED:RuntimeError")
        self.assertEqual(result["generation"], 0)
        self.assertNotIn("private validator detail", repr(result))
        self.assertIsNone(manager.active_config)

    def test_invalid_history_limits_are_rejected(self):
        for max_history in (True, 0, 101, 1.5):
            with self.subTest(max_history=max_history):
                with self.assertRaises(ValueError):
                    ProviderConfigLifecycleManager(valid_config, max_history=max_history)

    def test_rollback_restores_previous_known_good_generation(self):
        first = {"schema_version": "1", "providers": {"image": {}}}
        second = {"schema_version": "2", "providers": {"video": {}}}
        self.manager.reload(first)
        self.manager.reload(second)
        rolled = self.manager.rollback()
        self.assertEqual(rolled["status"], "CONFIG_ROLLED_BACK")
        self.assertEqual(self.manager.active_config, first)
        self.assertEqual(self.manager.generation, 3)

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

    def test_rollback_validation_failure_preserves_rollback_point(self):
        manager = ProviderConfigLifecycleManager(lambda config: config.get("valid") is True)
        first = {"schema_version": "1", "providers": {}, "valid": True}
        second = {"schema_version": "2", "providers": {}, "valid": True}
        manager.reload(first)
        manager.reload(second)
        manager.validator = lambda config: config.get("schema_version") == "2"

        rejected = manager.rollback()
        self.assertEqual(rejected["status"], "ROLLBACK_CONFIG_INVALID")
        self.assertEqual(manager.active_config, second)

        manager.validator = valid_config
        retried = manager.rollback()
        self.assertEqual(retried["status"], "CONFIG_ROLLED_BACK")
        self.assertEqual(manager.active_config, first)

    def test_migration_cycle_does_not_mutate_source_config(self):
        original = {"schema_version": "1", "providers": {}}

        def to_v2(config):
            config["schema_version"] = "2"
            return config

        def back_to_v1(config):
            config["schema_version"] = "1"
            return config

        result = self.manager.migrate(
            original,
            target_version="3",
            migrations={"1": to_v2, "2": back_to_v1},
        )
        self.assertEqual(result["status"], "MIGRATION_CYCLE_OR_LIMIT")
        self.assertFalse(result["migrated"])
        self.assertEqual(original, {"schema_version": "1", "providers": {}})

    def test_file_migration_write_failure_preserves_original_and_backup(self):
        path = self.root / "provider-config.json"
        original = {"schema_version": "1", "providers": {"image": {}}}
        original_text = json.dumps(original)
        path.write_text(original_text, encoding="utf-8")

        with patch(
            "projects.provider_store.provider_config_lifecycle.atomic_replace_with_retry",
            side_effect=OSError("simulated replacement failure"),
        ):
            result = self.manager.migrate_file(
                path, target_version="2", migrations={"1": migrate_v1_to_v2}
            )

        self.assertEqual(result["status"], "CONFIG_MIGRATION_WRITE_FAILED")
        self.assertFalse(result["migrated"])
        self.assertEqual(path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(
            json.loads(Path(str(path) + ".bak").read_text(encoding="utf-8")),
            original,
        )

    def test_concurrent_file_migrations_are_serialized_and_leave_valid_config(self):
        path = self.root / "concurrent-config.json"
        path.write_text(json.dumps({"schema_version": "1", "providers": {}}), encoding="utf-8")

        def to_v2(config):
            config["schema_version"] = "2"
            return config

        def to_v3(config):
            config["schema_version"] = "3"
            return config

        migrations = {"1": to_v2, "2": to_v3}
        managers = [
            ProviderConfigLifecycleManager(valid_config),
            ProviderConfigLifecycleManager(valid_config),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda manager: manager.migrate_file(
                    path, target_version="3", migrations=migrations
                ),
                managers,
            ))

        self.assertTrue(all(result["status"] == "CONFIG_MIGRATION_APPLIED" for result in results), results)
        final_config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(final_config["schema_version"], "3")
        self.assertIsInstance(final_config["providers"], dict)

    def test_invalid_config_file_is_not_overwritten(self):
        path = self.root / "bad.json"
        path.write_text("{broken", encoding="utf-8")
        result = self.manager.migrate_file(path, target_version="2", migrations={})
        self.assertEqual(result["status"], "INVALID_CONFIG_FILE")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


if __name__ == "__main__":
    unittest.main()
