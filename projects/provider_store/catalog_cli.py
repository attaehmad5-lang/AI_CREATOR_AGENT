"""Explicit, dry-run-first command-line tools for provider catalog maintenance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .catalog_diff import apply_catalog_candidate, diff_catalogs
from .catalog_interchange import (
    create_catalog_snapshot,
    export_catalog,
    import_catalog,
    list_catalog_snapshots,
    restore_catalog_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff = subparsers.add_parser("diff", help="compare catalogs without exposing field values")
    diff.add_argument("current", type=Path)
    diff.add_argument("candidate", type=Path)

    apply = subparsers.add_parser("apply", help="preview by default; apply only with --apply")
    apply.add_argument("current", type=Path)
    apply.add_argument("candidate", type=Path)
    apply.add_argument("--apply", action="store_true", dest="apply_changes")
    apply.add_argument("--allow-removals", action="store_true")

    export = subparsers.add_parser("export", help="atomically export a validated catalog")
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)

    imp = subparsers.add_parser("import", help="import a validated catalog with backup/rollback")
    imp.add_argument("source", type=Path)
    imp.add_argument("destination", type=Path)
    imp.add_argument("--no-backup", action="store_true", help="explicitly disable backup")

    snapshot = subparsers.add_parser("snapshot", help="create a validated immutable catalog snapshot")
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("history_directory", type=Path)

    history = subparsers.add_parser("history", help="list snapshot names/counts only")
    history.add_argument("history_directory", type=Path)

    restore = subparsers.add_parser("restore", help="restore a named validated snapshot")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("destination", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "diff":
            result = diff_catalogs(args.current, args.candidate)
        elif args.command == "apply":
            result = apply_catalog_candidate(
                args.current,
                args.candidate,
                dry_run=not args.apply_changes,
                allow_removals=args.allow_removals,
            )
        elif args.command == "export":
            result = export_catalog(args.source, args.destination)
        elif args.command == "import":
            result = import_catalog(args.source, args.destination, backup=not args.no_backup)
        elif args.command == "snapshot":
            result = create_catalog_snapshot(args.source, args.history_directory)
        elif args.command == "history":
            result = list_catalog_snapshots(args.history_directory)
        elif args.command == "restore":
            result = restore_catalog_snapshot(args.snapshot, args.destination)
        else:
            parser.error("unsupported command")
            return 2
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    if args.command == "apply" and result.get("status") == "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED":
        return 3
    if args.command == "import" and not result.get("imported"):
        return 1
    if args.command == "restore" and not result.get("restored"):
        return 1
    if args.command == "apply" and args.apply_changes and not result.get("applied"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
