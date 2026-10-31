"""Provider config migration, safe reload, and rollback tests."""
import json
import os
import tempfile
import unittest
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

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self):
        path = self.root / "provider-config.json"
        original = {"schema_version": "1", "providers": {"image": {}}}
        original_bytes = json.dumps(original).encode("utf-8")
        path.write_bytes(original_bytes)

        from unittest.mock import patch
        with patch(
            "projects.provider_store.provider_config_lifecycle.atomic_replace_with_retry",
            side_effect=OSError("simulated replace failure"),
        ):
            result = self.manager.migrate_file(
                path, target_version="2", migrations={"1": migrate_v1_to_v2}
            )

        self.assertEqual(result["status"], "CONFIG_MIGRATION_WRITE_FAILED")
        self.assertFalse(result["migrated"])
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertTrue(Path(str(path) + ".bak").is_file())
        self.assertEqual(list(self.root.glob(".provider-config.json.*.tmp")), [])

    def test_rollback_does_not_discard_previous_config_when_validation_fails(self):
        first = {"schema_version": "1", "providers": {"image": {}}}
        second = {"schema_version": "2", "providers": {"video": {}}}
        self.manager.reload(first)
        self.manager.reload(second)
        original_validator = self.manager.validator

        def reject_previous(config):
            return config.get("schema_version") == "2"

        self.manager.validator = reject_previous
        result = self.manager.rollback()
        self.assertEqual(result["status"], "ROLLBACK_CONFIG_INVALID")
        self.assertEqual(self.manager.active_config, second)
        self.assertEqual(len(self.manager._history), 1)
        self.manager.validator = original_validator
        recovered = self.manager.rollback()
        self.assertEqual(recovered["status"], "CONFIG_ROLLED_BACK")
        self.assertEqual(self.manager.active_config, first)

    def test_invalid_config_file_is_not_overwritten(self):
        path = self.root / "bad.json"
        path.write_text("{broken", encoding="utf-8")
        result = self.manager.migrate_file(path, target_version="2", migrations={})
        self.assertEqual(result["status"], "INVALID_CONFIG_FILE")
        self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


    def test_failed_file_rollback_cleans_restore_temp_and_keeps_backup(self):
        path = self.root / "provider-config.json"
        original = {"schema_version": "1", "providers": {"image": {}}}
        original_bytes = json.dumps(original).encode("utf-8")
        path.write_bytes(original_bytes)

        from unittest.mock import patch
        replacements = {"count": 0}

        def fail_restore(source, destination):
            replacements["count"] += 1
            if replacements["count"] == 1:
                os.replace(source, destination)
                return
            raise OSError("simulated rollback replacement failure")

        with (
            patch.object(self.manager, "reload", return_value={"reloaded": False}),
            patch(
                "projects.provider_store.provider_config_lifecycle.atomic_replace_with_retry",
                side_effect=fail_restore,
            ),
        ):
            result = self.manager.migrate_file(
                path, target_version="2", migrations={"1": migrate_v1_to_v2}
            )

        self.assertEqual(result["status"], "CONFIG_MIGRATION_ROLLBACK_FAILED")
        self.assertFalse(result["migrated"])
        self.assertEqual(
            json.loads(Path(str(path) + ".bak").read_text(encoding="utf-8")),
            original,
        )
        self.assertEqual(list(self.root.glob(".provider-config.json.restore.*.tmp")), [])
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["schema_version"], "2"
        )


if __name__ == "__main__":
    unittest.main()
