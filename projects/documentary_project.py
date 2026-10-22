"""Create the standard AI Creator Agent documentary workspace folders.

The default root is anchored to this file, not the caller's current working
directory. Importing this module has no filesystem side effects.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

PROJECT_NAME = "World Most Popular Documentary Filming"
DEFAULT_FOLDERS = (
    "research",
    "story",
    "scenes",
    "visuals",
    "voice",
    "video",
    "seo",
)


def create_project_structure(
    root: str | Path | None = None,
    folders: Iterable[str] = DEFAULT_FOLDERS,
) -> list[Path]:
    """Create and return the requested folders, safely and idempotently.

    Args:
        root: Folder under which the structure is created. Defaults to the
            directory containing this module (the repository's projects/ dir).
        folders: Relative folder names. Absolute paths and traversal are
            rejected to prevent writing outside root.
    """
    base = Path(root).expanduser().resolve() if root is not None else Path(__file__).resolve().parent
    base.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    for name in folders:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("folder names must be non-empty strings")
        candidate = Path(name)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise ValueError(f"unsafe folder path: {name!r}")
        if candidate == Path("."):
            raise ValueError("folder name must not be the root itself")

        target = (base / candidate).resolve()
        if target != base and base not in target.parents:
            raise ValueError(f"folder escapes root: {name!r}")
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise NotADirectoryError(f"expected a directory: {target}")
        created.append(target)
    return created


def main() -> int:
    print()
    print("===================================")
    print("      DOCUMENTARY PROJECT")
    print("===================================")
    print()
    print("Project:", PROJECT_NAME)
    print("Root:", Path(__file__).resolve().parent)
    print()

    try:
        for path in create_project_structure():
            print("[READY]", path)
    except (OSError, ValueError) as exc:
        print("[ERROR]", exc)
        return 1

    print()
    print("Documentary project structure verified successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
