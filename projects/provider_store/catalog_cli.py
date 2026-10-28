"""Explicit, dry-run-first command-line tools for provider catalog maintenance."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .catalog_diff import apply_catalog_candidate, diff_catalogs
from .provider_audit_log import ProviderAuditLog
from .catalog_interchange import (
    create_catalog_snapshot,
    export_catalog,
    import_catalog,
    list_catalog_snapshots,
    prune_catalog_snapshots,
    restore_catalog_snapshot,
)


def _add_audit_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="append hash-chained audit events here and refuse commands if the audit log cannot be written",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    diff = subparsers.add_parser("diff", help="compare catalogs without exposing field values")
    _add_audit_option(diff)
    diff.add_argument("current", type=Path)
    diff.add_argument("candidate", type=Path)

    apply = subparsers.add_parser("apply", help="preview by default; apply only with --apply")
    _add_audit_option(apply)
    apply.add_argument("current", type=Path)
    apply.add_argument("candidate", type=Path)
    apply.add_argument("--apply", action="store_true", dest="apply_changes")
    apply.add_argument("--allow-removals", action="store_true")

    export = subparsers.add_parser("export", help="atomically export a validated catalog")
    _add_audit_option(export)
    export.add_argument("source", type=Path)
    export.add_argument("destination", type=Path)

    imp = subparsers.add_parser("import", help="import a validated catalog with backup/rollback")
    _add_audit_option(imp)
    imp.add_argument("source", type=Path)
    imp.add_argument("destination", type=Path)
    imp.add_argument("--no-backup", action="store_true", help="explicitly disable backup")

    snapshot = subparsers.add_parser("snapshot", help="create a validated immutable catalog snapshot")
    _add_audit_option(snapshot)
    snapshot.add_argument("source", type=Path)
    snapshot.add_argument("history_directory", type=Path)

    history = subparsers.add_parser("history", help="list snapshot names/counts only")
    _add_audit_option(history)
    history.add_argument("history_directory", type=Path)

    restore = subparsers.add_parser("restore", help="restore a named validated snapshot")
    _add_audit_option(restore)
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("destination", type=Path)

    prune = subparsers.add_parser(
        "prune", help="preview old-snapshot retention; deletion requires --apply"
    )
    _add_audit_option(prune)
    prune.add_argument("history_directory", type=Path)
    prune.add_argument("--keep", type=int, default=20)
    prune.add_argument("--apply", action="store_true", dest="apply_changes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    audit = None
    if args.audit_log is not None:
        try:
            audit = ProviderAuditLog(args.audit_log)
            started = audit.record(
                f"catalog.{args.command}.started",
                status="COMMAND_STARTED",
                outcome="UNKNOWN",
            )
        except (OSError, ValueError):
            started = {"status": "AUDIT_LOG_CONFIGURATION_FAILED", "saved": False}
        if started.get("saved") is not True:
            print(json.dumps({
                "status": "AUDIT_LOG_REQUIRED",
                "operation": args.command,
                "audit": {"status": started.get("status", "AUDIT_LOG_WRITE_FAILED"), "saved": False},
            }, indent=2, sort_keys=True))
            return 2
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
        elif args.command == "prune":
            result = prune_catalog_snapshots(
                args.history_directory, keep=args.keep, apply=args.apply_changes
            )
        else:
            parser.error("unsupported command")
            return 2
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2
    exit_code = 0
    if args.command == "apply" and result.get("status") == "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED":
        exit_code = 3
    elif args.command == "import" and not result.get("imported"):
        exit_code = 1
    elif args.command == "restore" and not result.get("restored"):
        exit_code = 1
    elif args.command == "apply" and args.apply_changes and not result.get("applied"):
        exit_code = 1
    if audit is not None:
        status = result.get("status", "UNKNOWN")
        if not isinstance(status, str) or not status:
            status = "UNKNOWN"
        outcome = "DENIED" if exit_code == 3 else ("ERROR" if exit_code else "OK")
        try:
            completed = audit.record(
                f"catalog.{args.command}.completed",
                status=status,
                outcome=outcome,
                reason_code=status if outcome != "OK" else None,
            )
        except Exception:
            completed = {"status": "AUDIT_LOG_WRITE_FAILED", "saved": False}
        result["audit"] = {
            "status": completed.get("status", "AUDIT_LOG_WRITE_FAILED"),
            "saved": completed.get("saved") is True,
        }
        if completed.get("saved") is not True and exit_code == 0:
            exit_code = 4
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
