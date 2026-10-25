"""CI guard for tracked local credentials and common hard-coded token formats."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REQUIRED_IGNORE_PATTERNS = {"hf_token.txt", ".env", ".env.*", "!.env.example", "hf_token_setup.py"}
FORBIDDEN_BASENAMES = {"hf_token.txt", "hf_token_setup.py", ".env"}
# Keep these patterns split across adjacent strings so this scanner does not
# match its own source when inspecting tracked files.
SECRET_PATTERNS = (
    re.compile("hf_" + r"[A-Za-z0-9]{30,}"),
    re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
    re.compile("github_pat" + r"_[A-Za-z0-9_]{50,}"),
    re.compile("AKIA" + r"[A-Z0-9]{16}"),
    re.compile("xox" + r"[baprs]-[A-Za-z0-9-]{20,}"),
)
MAX_SCAN_BYTES = 2 * 1024 * 1024
SCANNER_PATH = "tests/check_secret_exclusions.py"


def main() -> int:
    ignore_path = Path(".gitignore")
    if not ignore_path.is_file():
        print("FAIL: .gitignore is missing", file=sys.stderr)
        return 1
    ignore_lines = {
        line.strip()
        for line in ignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = sorted(REQUIRED_IGNORE_PATTERNS - ignore_lines)
    if missing:
        print("FAIL: required secret-exclusion patterns missing: " + ", ".join(missing), file=sys.stderr)
        return 1

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        print("FAIL: unable to enumerate tracked files for secret checks", file=sys.stderr)
        return 1

    tracked = [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\\0")
        if item
    ]
    forbidden = []
    for path in tracked:
        name = Path(path).name
        if name in FORBIDDEN_BASENAMES or (
            name.startswith(".env.") and name != ".env.example"
        ):
            forbidden.append(path)
    if forbidden:
        print(
            "FAIL: local credential/setup files must not be tracked: "
            + ", ".join(sorted(forbidden)),
            file=sys.stderr,
        )
        return 1

    # Scan small UTF-8 tracked text files for common provider/source-control
    # tokens. Report locations only; never print a matched value to CI logs.
    findings = []
    for path in tracked:
        if path == SCANNER_PATH:
            continue
        try:
            file_path = Path(path)
            if not file_path.is_file() or file_path.stat().st_size > MAX_SCAN_BYTES:
                continue
            raw = file_path.read_bytes()
            if b"\\0" in raw:
                continue
            source = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(source.splitlines(), start=1):
            if any(pattern.search(line) for pattern in SECRET_PATTERNS):
                findings.append(f"{path}:{line_number}")
    if findings:
        print(
            "FAIL: possible hard-coded credential/token pattern found at: "
            + ", ".join(sorted(set(findings))),
            file=sys.stderr,
        )
        return 1

    print(
        "PASS: required ignore patterns exist; known local credential/setup files "
        "are untracked; no common hard-coded token patterns found in scanned text files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
