"""Create the standard AI Creator Agent documentary workspace folders.

The default root is anchored to this file, not the caller's current working
directory. Importing this module has no filesystem side effects.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from projects.folder_engine import FolderEngine

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
    engine = FolderEngine(base)
    entries = engine.ensure_folders(folders)
    return [entry.absolute_path for entry in entries]


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
