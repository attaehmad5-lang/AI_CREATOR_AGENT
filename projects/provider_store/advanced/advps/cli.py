"""Command line interface: advps --home DIR <command>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLog
from .catalog import Catalog
from .errors import AdvpsError
from .redact import redact_text
from .router import Router
from .storage import AtomicJsonStore


def _open(home: str):
    base = Path(home)
    store = AtomicJsonStore(base / "catalog.json")
    audit = AuditLog(base / "audit.jsonl")
    return store, Catalog(store, audit=audit), audit


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="advps")
    p.add_argument("--home", default=".advps")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("json_file"); a.add_argument("--replace", action="store_true")
    l = sub.add_parser("list"); l.add_argument("--capability"); l.add_argument("--enabled-only", action="store_true")
    for name in ("enable", "disable", "remove"):
        sub.add_parser(name).add_argument("id")
    sub.add_parser("export")
    i = sub.add_parser("import"); i.add_argument("file")
    sub.add_parser("backup")
    r = sub.add_parser("restore"); r.add_argument("backup_file")
    sub.add_parser("verify")
    s = sub.add_parser("simulate"); s.add_argument("capability"); s.add_argument("--strategy", default="priority")
    s.add_argument("--region")
    sub.add_parser("diagnose")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store, cat, audit = _open(args.home)
        if args.cmd == "add":
            rec = json.loads(Path(args.json_file).read_text("utf-8"))
            print(json.dumps(cat.register(rec, replace=args.replace), indent=2))
        elif args.cmd == "list":
            for p in cat.list(args.capability, args.enabled_only):
                print(f"{p['id']}\t{p['version']}\t{'enabled' if p['enabled'] else 'disabled'}")
        elif args.cmd in ("enable", "disable"):
            cat.set_enabled(args.id, args.cmd == "enable")
        elif args.cmd == "remove":
            cat.unregister(args.id)
        elif args.cmd == "export":
            print(cat.export_json())
        elif args.cmd == "import":
            print(cat.import_json(Path(args.file).read_text("utf-8")))
        elif args.cmd == "backup":
            print(store.backup())
        elif args.cmd == "restore":
            store.restore(args.backup_file)
        elif args.cmd == "verify":
            store.read()
            print(f"catalog ok (revision {cat.revision}); audit entries verified: {audit.verify()}")
        elif args.cmd == "simulate":
            print(json.dumps(Router(cat).simulate(args.capability, strategy=args.strategy, region=args.region)))
        elif args.cmd == "diagnose":
            recovered = store.recover()
            print(json.dumps({"providers": len(cat.list()), "revision": cat.revision,
                              "integrity": cat.integrity_hash(), "recovered_from_backup": recovered,
                              "backups": len(store.list_backups()), "stale_tmp_removed": store.cleanup_temp()}))
        return 0
    except AdvpsError as exc:
        print(f"error[{exc.code}]: {redact_text(str(exc))}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"error: {redact_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())  # pragma: no cover
