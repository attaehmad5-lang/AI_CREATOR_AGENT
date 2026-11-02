"""Repository-level guard against committing local credentials and generated assets."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IGNORE_RULES = (
    "hf_token.txt",
    ".env",
    ".env.*",
    "!.env.example",
    "__pycache__/",
    "*.py[cod]",
    "hf_token_setup.py",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.webp",
    "*.mp4",
    "*.mp3",
    "*.wav",
    "*.zip",
)
FORBIDDEN_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".png", ".jpg", ".jpeg",
    ".webp", ".mp4", ".mp3", ".wav", ".zip",
}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _tracked_paths() -> list[str] | None:
    result = _git("ls-files", "-z")
    if result.returncode != 0:
        return None
    return [
        item.decode("utf-8", "surrogateescape").replace("\\", "/")
        for item in result.stdout.split(b"\x00")
        if item
    ]


def _forbidden_tracked_path(path: str) -> bool:
    normalized = path.casefold()
    parsed = PurePosixPath(normalized)
    name = parsed.name
    if name in {"hf_token.txt", "hf_token_setup.py", ".env"}:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if "__pycache__" in parsed.parts or parsed.suffix in FORBIDDEN_SUFFIXES:
        return True
    return False


class SecretExclusionAuditTests(unittest.TestCase):
    def test_gitignore_contains_required_credential_and_asset_rules(self):
        ignore_file = ROOT / ".gitignore"
        self.assertTrue(ignore_file.is_file(), ".gitignore is missing")
        rules = {
            line.strip()
            for line in ignore_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for rule in REQUIRED_IGNORE_RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, rules)

    def test_sensitive_and_generated_paths_are_actually_ignored_by_git(self):
        samples = (
            "hf_token.txt",
            "nested/hf_token.txt",
            ".env",
            ".env.local",
            "hf_token_setup.py",
            "__pycache__/module.cpython-313.pyc",
            "cover.png",
            "nested/poster.jpg",
            "video.mp4",
            "audio.wav",
            "backup.zip",
        )
        for sample in samples:
            with self.subTest(path=sample):
                result = _git("check-ignore", "--no-index", "--quiet", "--", sample)
                self.assertEqual(
                    result.returncode, 0,
                    f"{sample} is not ignored by the repository's active .gitignore",
                )
        allowed = _git("check-ignore", "--no-index", "--quiet", "--", ".env.example")
        self.assertEqual(allowed.returncode, 1, ".env.example must remain eligible for tracking")

    def test_no_sensitive_or_generated_paths_are_in_the_git_index(self):
        paths = _tracked_paths()
        if paths is None:
            self.skipTest("Git index is unavailable outside a checked-out repository")
        forbidden = [path for path in paths if _forbidden_tracked_path(path)]
        self.assertEqual(forbidden, [], "Forbidden paths are already tracked: " + repr(forbidden))


if __name__ == "__main__":
    unittest.main(verbosity=2)
