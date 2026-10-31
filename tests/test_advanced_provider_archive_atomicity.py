import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.advanced.release_report import write_release_artifacts


def ledger():
    rows = [
        "# Ledger",
        "| # | Feature | Status | Evidence | Remaining work |",
        "|---:|---|---|---|---|",
    ]
    for number in range(1, 151):
        status = "COMPLETED" if number == 1 else "PARTIAL"
        rows.append(
            f"| {number:03d} | Feature {number} | {status} | test evidence | continue audit |"
        )
    return "\n".join(rows) + "\n"


class AtomicArchiveTests(unittest.TestCase):
    def test_archive_replace_failure_preserves_previous_zip_and_cleans_temporary_file(self):
        import os

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = root / "projects" / "provider_store"
            advanced = provider / "advanced"
            advanced.mkdir(parents=True)
            (advanced / "ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md").write_text(
                ledger(), encoding="utf-8"
            )
            source = provider / "safe.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            output = root / "build"
            first = write_release_artifacts(output, repo_root=root)
            archive = Path(first["archive"])
            previous = archive.read_bytes()
            source.write_text("VALUE = 2\n", encoding="utf-8")

            original_replace = os.replace

            def fail_archive_replace(source_path, destination_path):
                if Path(destination_path) == archive:
                    raise OSError("injected archive replace failure")
                return original_replace(source_path, destination_path)

            with patch(
                "projects.provider_store.advanced.release_report.os.replace",
                side_effect=fail_archive_replace,
            ):
                with self.assertRaisesRegex(OSError, "injected archive replace failure"):
                    write_release_artifacts(output, repo_root=root)

            self.assertEqual(archive.read_bytes(), previous)
            temporary_archives = list(output.glob(f".{archive.name}.*.tmp"))
            self.assertEqual(temporary_archives, [])


if __name__ == "__main__":
    unittest.main()
