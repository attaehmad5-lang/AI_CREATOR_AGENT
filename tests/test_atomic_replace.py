import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.interprocess_lock import atomic_replace_with_retry


class AtomicReplaceRetryTests(unittest.TestCase):
    def test_transient_permission_error_is_retried_and_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "temporary.json"
            destination = root / "state.json"
            source.write_text('{"value": 2}', encoding="utf-8")
            destination.write_text('{"value": 1}', encoding="utf-8")
            real_replace = os.replace
            calls = [0]

            def flaky_replace(src, dst):
                calls[0] += 1
                if calls[0] < 3:
                    raise PermissionError("transient sharing violation")
                return real_replace(src, dst)

            with patch(
                "projects.provider_store.interprocess_lock.os.replace",
                side_effect=flaky_replace,
            ), patch("projects.provider_store.interprocess_lock.time.sleep"):
                atomic_replace_with_retry(source, destination)

            self.assertEqual(calls[0], 3)
            self.assertEqual(destination.read_text(encoding="utf-8"), '{"value": 2}')
            self.assertFalse(source.exists())

    def test_persistent_permission_error_is_raised_after_bounded_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "temporary.json"
            destination = root / "state.json"
            source.write_text("new", encoding="utf-8")
            real_replace = os.replace
            calls = [0]

            def blocked_replace(src, dst):
                calls[0] += 1
                raise PermissionError("still locked")

            with patch(
                "projects.provider_store.interprocess_lock.os.replace",
                side_effect=blocked_replace,
            ), patch("projects.provider_store.interprocess_lock.time.sleep"):
                with self.assertRaises(PermissionError):
                    atomic_replace_with_retry(source, destination)

            self.assertEqual(calls[0], 8)
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
