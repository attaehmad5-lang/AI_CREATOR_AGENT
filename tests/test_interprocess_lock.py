import tempfile
import unittest
from pathlib import Path

from projects.provider_store.interprocess_lock import interprocess_file_lock


class InterprocessFileLockTests(unittest.TestCase):
    def test_rejects_symlink_lock_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.bin"
            target.write_bytes(b"protected-target")
            link = root / "provider.lock"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this platform")

            with self.assertRaises(OSError):
                with interprocess_file_lock(link):
                    self.fail("symlink lock should never be acquired")

            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), b"protected-target")

    def test_creates_persistent_regular_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "provider.lock"
            with interprocess_file_lock(lock):
                self.assertTrue(lock.is_file())
                self.assertFalse(lock.is_symlink())
                self.assertEqual(lock.stat().st_size, 1)
            self.assertTrue(lock.exists(), "lock file must persist to avoid split-lock races")


if __name__ == "__main__":
    unittest.main()
