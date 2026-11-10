"""Offline disaster recovery acceptance. Never contacts providers or spends credits."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import Mock, patch

from agents.jobs.artifact_integrity import ArtifactIntegrityError, ArtifactVerifier
from agents.jobs.disaster_recovery import (
    RecoveryError, create_verified_backup, restore_verified_backup,
    verify_verified_backup,
)
from agents.jobs.job_engine import MasterJobEngine


KEY = b"test-only-recovery-signing-material-32!"  # Do not use in production.


class DisasterRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.database = self.folder / "live.sqlite3"
        self.backup = self.folder / "backup.sqlite3"
        self.restored = self.folder / "restored.sqlite3"
        self.engine = MasterJobEngine(self.database)
        self.job = self.engine.submit_request({"topic": "Sensitive project title"}, idempotency_key="stable-id")
        self.engine.run_job(self.job)

    def make_backup(self):
        return create_verified_backup(self.database, self.backup, signing_key=KEY)

    def test_connection_setup_exception_closes_sqlite_handle(self):
        # PRAGMA setup can fail under local lock contention or injected I/O errors.
        fake = Mock()
        fake.execute.side_effect = sqlite3.OperationalError("simulated sqlite setup failure")
        with patch("agents.jobs.job_engine.sqlite3.connect", return_value=fake):
            with self.assertRaises(sqlite3.OperationalError):
                with self.engine._db():
                    pass
        fake.close.assert_called_once()

    def test_signed_wal_consistent_backup_restore_preserves_workflow_and_deduplication(self):
        expected_events = self.engine.job_events(self.job["job_id"])
        evidence = self.make_backup()
        self.assertEqual(evidence["schema_version"], 3)
        self.assertEqual(evidence["row_counts"]["jobs"], 1)
        self.assertEqual(evidence["row_counts"]["submission_keys"], 1)
        self.assertEqual(evidence["row_counts"]["job_events"], len(expected_events))
        self.assertEqual(verify_verified_backup(self.backup, signing_key=KEY), evidence)
        restored = restore_verified_backup(self.backup, self.restored, signing_key=KEY)
        self.assertTrue(restored["restored"])
        recovered_engine = MasterJobEngine(self.restored)
        self.assertEqual(recovered_engine.load_job(self.job["job_id"])["status"], "WAITING")
        self.assertEqual(recovered_engine.job_events(self.job["job_id"]), expected_events)
        self.assertEqual(recovered_engine.submit_request({"topic": "Sensitive project title"},
                        idempotency_key="stable-id")["job_id"], self.job["job_id"])
        self.assertNotIn(b"Sensitive project title", self.backup.with_name(self.backup.name + ".manifest.json").read_bytes())

    def test_digest_detects_bit_changes_to_sqlite(self):
        self.make_backup()
        with self.backup.open("r+b") as handle:
            handle.seek(200)
            before = handle.read(1)
            handle.seek(200)
            handle.write(bytes([before[0] ^ 1]))
        with self.assertRaisesRegex(RecoveryError, "no longer matches manifest"):
            verify_verified_backup(self.backup, signing_key=KEY)
        with self.assertRaises(RecoveryError):
            restore_verified_backup(self.backup, self.restored, signing_key=KEY)
        self.assertFalse(self.restored.exists())

    def test_manifest_tamper_detected_even_if_attacker_knows_hash(self):
        self.make_backup()
        manifest = self.backup.with_name(self.backup.name + ".manifest.json")
        forged = json.loads(manifest.read_text())
        forged["row_counts"]["jobs"] = 10
        manifest.write_text(json.dumps(forged))
        with self.assertRaisesRegex(RecoveryError, "signature"):
            verify_verified_backup(self.backup, signing_key=KEY)

    def test_wrong_key_missing_key_and_weak_key_are_rejected(self):
        self.make_backup()
        for key in (None, b"wrong", b"w" * 32):
            with self.subTest(key=key), self.assertRaises(RecoveryError):
                verify_verified_backup(self.backup, signing_key=key)

    def test_unsigned_backup_requires_explicit_both_sides_opt_in(self):
        with self.assertRaises(RecoveryError):
            create_verified_backup(self.database, self.backup)
        create_verified_backup(self.database, self.backup, require_signed=False)
        with self.assertRaises(RecoveryError):
            verify_verified_backup(self.backup)
        self.assertEqual(verify_verified_backup(self.backup, require_signed=False)["schema_version"], 3)
        self.assertTrue(restore_verified_backup(self.backup, self.restored, require_signed=False)["restored"])

    def test_create_never_overwrites_original_backup_or_manifest(self):
        self.make_backup()
        before = self.backup.read_bytes()
        with self.assertRaises(RecoveryError):
            self.make_backup()
        self.assertEqual(self.backup.read_bytes(), before)

    def test_restore_never_overwrites_existing_database(self):
        self.make_backup()
        self.restored.write_bytes(b"existing database")
        with self.assertRaisesRegex(RecoveryError, "must not exist"):
            restore_verified_backup(self.backup, self.restored, signing_key=KEY)
        self.assertEqual(self.restored.read_bytes(), b"existing database")

    def test_invalid_manifest_and_bad_database_fail_closed(self):
        self.make_backup()
        manifest = self.backup.with_name(self.backup.name + ".manifest.json")
        manifest.write_bytes(b"{" + b"a" * 5000)
        with self.assertRaisesRegex(RecoveryError, "manifest too large"):
            verify_verified_backup(self.backup, signing_key=KEY)
        other = self.folder / "not-sqlite.sqlite3"
        other.write_bytes(b"not a valid database")
        with self.assertRaises(RecoveryError):
            create_verified_backup(other, self.folder / "other.backup", signing_key=KEY)

    def test_unsupported_db_version_is_refused_without_output(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(RecoveryError, "Unsupported master job database version"):
            self.make_backup()
        self.assertFalse(self.backup.exists())

    def test_backup_symlink_paths_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("Symlinks unavailable")
        alias = self.folder / "live-link.sqlite3"
        try:
            alias.symlink_to(self.database)
        except OSError:
            self.skipTest("Symlink creation prohibited")
        with self.assertRaises(RecoveryError):
            create_verified_backup(alias, self.backup, signing_key=KEY)
        self.make_backup()
        with self.assertRaises(RecoveryError):
            restore_verified_backup(self.backup, alias, signing_key=KEY)
        with self.assertRaises(RecoveryError):
            create_verified_backup(self.database, alias, signing_key=KEY)

    def test_concurrent_same_target_never_unlinks_winning_backup(self):
        errors, successes = [], []
        barrier = threading.Barrier(3)
        def writer():
            barrier.wait()
            try:
                successes.append(create_verified_backup(self.database, self.backup, signing_key=KEY))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join(15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(verify_verified_backup(self.backup, signing_key=KEY), successes[0])

    def test_cli_requires_environment_secret_and_produces_metadata_only(self):
        env = os.environ.copy()
        env.pop("AI_CREATOR_BACKUP_KEY_HEX", None)
        def command(*args, env=env):
            return subprocess.run([sys.executable, "-m", "agents.jobs.disaster_recovery", *map(str, args)],
                                  text=True, capture_output=True, env=env)
        without = command("backup", self.database, self.backup)
        self.assertEqual(without.returncode, 2)
        env_signed = {**env, "AI_CREATOR_BACKUP_KEY_HEX": KEY.hex()}
        produced = command("backup", self.database, self.backup, env=env_signed)
        self.assertEqual(produced.returncode, 0, produced.stderr)
        self.assertNotIn("Sensitive project title", produced.stdout)
        verified = command("verify", self.backup, env=env_signed)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        restored = command("restore", self.backup, self.restored, env=env_signed)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIsNotNone(MasterJobEngine(self.restored).load_job(self.job["job_id"]))

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_artifact_fifo_rejected_immediately_without_blocking(self):
        # An untrusted file type must not block the worker *before* fstat.
        media = self.folder / "media"
        media.mkdir()
        os.mkfifo(media / "pipe.png")
        verifier = ArtifactVerifier(media)
        with self.assertRaises(ArtifactIntegrityError):
            verifier.inspect("pipe.png", "image/png")


if __name__ == "__main__":
    unittest.main()
