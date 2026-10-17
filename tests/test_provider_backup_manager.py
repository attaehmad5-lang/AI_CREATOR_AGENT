import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from projects.provider_store.provider_backup_manager import ProviderBackupManager


class ProviderBackupManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "root"
        self.backups = Path(self.temp_dir.name) / "backups"
        self.root.mkdir()
        (self.root / "config.json").write_text(
            json.dumps({"providers": [{"id": "image", "enabled": True}]}),
            encoding="utf-8",
        )
        (self.root / "quota.json").write_text(
            json.dumps({"requests": {"job-1": {"status": "COMMITTED"}}}),
            encoding="utf-8",
        )
        self.manager = ProviderBackupManager(self.root, self.backups, max_file_bytes=4096, max_total_bytes=8192)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_backup_verify_and_restore_json_state(self):
        created = self.manager.create_backup(name="before-change", files=["config.json", "quota.json"])
        self.assertEqual(created["status"], "BACKUP_CREATED")
        self.assertEqual(created["file_count"], 2)
        verified = self.manager.verify_backup(name="before-change")
        self.assertTrue(verified["verified"])
        (self.root / "config.json").write_text('{"providers":[]}', encoding="utf-8")
        blocked = self.manager.restore_backup(name="before-change")
        self.assertEqual(blocked["status"], "RESTORE_TARGET_EXISTS")
        restored = self.manager.restore_backup(name="before-change", overwrite=True)
        self.assertEqual(restored["status"], "RESTORED")
        self.assertEqual(json.loads((self.root / "config.json").read_text(encoding="utf-8"))["providers"][0]["id"], "image")

    def test_duplicate_backup_and_invalid_name_are_rejected(self):
        self.assertEqual(self.manager.create_backup(name="../escape", files=["config.json"])["status"], "INVALID_BACKUP_NAME")
        self.assertEqual(self.manager.create_backup(name="good", files=["config.json"])["status"], "BACKUP_CREATED")
        self.assertEqual(self.manager.create_backup(name="good", files=["config.json"])["status"], "BACKUP_ALREADY_EXISTS")

    def test_traversal_duplicate_and_secret_paths_are_rejected(self):
        for index, files in enumerate((["../outside.json"], ["/etc/passwd.json"], ["config.json", "config.json"], ["api_token.json"])):
            with self.subTest(files=files):
                result = self.manager.create_backup(name=f"unsafe-{index}", files=files)
                self.assertIn(result["status"], {"UNSAFE_BACKUP_PATH", "INVALID_BACKUP_FILE_LIST"})

    def test_secret_bearing_json_and_non_json_files_are_rejected(self):
        (self.root / "unsafe.json").write_text('{"api_key":"never-backup"}', encoding="utf-8")
        self.assertEqual(
            self.manager.create_backup(name="unsafe-content", files=["unsafe.json"])["status"],
            "BACKUP_SOURCE_INVALID_OR_SENSITIVE",
        )
        (self.root / "notes.txt").write_text("text", encoding="utf-8")
        self.assertEqual(
            self.manager.create_backup(name="bad-ext", files=["notes.txt"])["status"],
            "UNSAFE_BACKUP_PATH",
        )

    def test_modified_archive_content_fails_checksum_verification(self):
        self.manager.create_backup(name="tamper", files=["config.json"])
        archive_path = self.backups / "tamper.zip"
        replacement = self.temp_dir.name + "/replacement.zip"
        with zipfile.ZipFile(archive_path, "r") as source:
            entries = {name: source.read(name) for name in source.namelist()}
        entries["files/config.json"] = b'{"providers":[]}'
        with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, data in entries.items():
                target.writestr(name, data)
        archive_path.write_bytes(Path(replacement).read_bytes())
        result = self.manager.verify_backup(name="tamper")
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "BACKUP_CHECKSUM_OR_CONTENT_INVALID")

    def test_corrupt_archive_and_missing_backup_fail_closed(self):
        self.assertEqual(self.manager.verify_backup(name="missing")["status"], "BACKUP_NOT_FOUND")
        (self.backups).mkdir(exist_ok=True)
        (self.backups / "corrupt.zip").write_bytes(b"not-a-zip")
        self.assertEqual(self.manager.verify_backup(name="corrupt")["status"], "BACKUP_ARCHIVE_INVALID")
        self.assertEqual(self.manager.restore_backup(name="corrupt", overwrite=True)["status"], "BACKUP_NOT_VERIFIED")

    def test_invalid_limits_and_flags_are_rejected(self):
        with self.assertRaises(ValueError):
            ProviderBackupManager(self.root, self.backups, max_file_bytes=5000, max_total_bytes=1000)
        self.assertEqual(
            self.manager.restore_backup(name="none", overwrite="yes")["status"],
            "INVALID_OVERWRITE_FLAG",
        )


if __name__ == "__main__":
    unittest.main()
