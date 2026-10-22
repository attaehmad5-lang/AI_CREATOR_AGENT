"""CI guard: fail if local provider credentials/setup secrets are tracked."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REQUIRED_IGNORE_PATTERNS = {"hf_token.txt", ".env", ".env.*", "hf_token_setup.py"}
FORBIDDEN_BASENAMES = {"hf_token.txt", "hf_token_setup.py", ".env"}


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

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    tracked = [
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
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
    print("PASS: required ignore patterns exist and no known local credential/setup files are tracked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
