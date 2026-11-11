"""Consistent, authenticated SQLite backup and safe, create-only restore.

No provider calls. Backups contain sensitive job snapshots; keep them encrypted at
rest and restrict backup/manifest paths to trusted operators. HMAC authenticates
backup bytes + metadata, not the operator's identity or the host filesystem.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

_FORMAT = "ai-creator-master-jobs-backup-v1"
_TABLES = ("jobs", "submission_keys", "job_events", "external_dispatches")
_LIMIT = 8 * 1024 * 1024 * 1024
_MAX_MANIFEST_BYTES = 4096


class RecoveryError(ValueError):
    """Backup/restore refused without changing the source database."""


def _key(key: bytes | None, *, require_signed: bool) -> None:
    if key is not None and (type(key) is not bytes or len(key) < 32):
        raise RecoveryError("Signing key must have at least 32 bytes")
    if require_signed and key is None:
        raise RecoveryError("Signed backups require a signing key")


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _mac(payload: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, b"master-job-db-backup-v1\x00" + _canonical(payload),
                    hashlib.sha256).hexdigest()


def _file(path: str | os.PathLike[str], *, required: bool = True) -> Path:
    target = Path(path).absolute()
    if target.is_symlink():
        raise RecoveryError("Symlink backup/database paths are prohibited")
    if required and (not target.exists() or not target.is_file()):
        raise RecoveryError("Backup/database must be a regular file")
    if target.exists() and not stat.S_ISREG(target.stat().st_mode):
        raise RecoveryError("Backup/database must be a regular file")
    return target


def _fresh_file(path: Path) -> None:
    """Create a file owned by the current account; fail on name collisions."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.close(fd)


def _digest(path: Path) -> tuple[int, str]:
    count = 0
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RecoveryError("Not a regular backup file")
        while True:
            piece = stream.read(1024 * 1024)
            if not piece:
                break
            count += len(piece)
            if count > _LIMIT:
                raise RecoveryError("Backup exceeds maximum size")
            sha.update(piece)
    if count == 0:
        raise RecoveryError("Empty backup")
    return count, sha.hexdigest()


def _inspect_database(path: Path) -> dict[str, int]:
    # Open read-only: malformed/corrupt backup must never be mutated in place.
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RecoveryError("SQLite integrity_check failed")
            if db.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise RecoveryError("Unsupported master job database version")
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(_TABLES).issubset(tables):
                raise RecoveryError("Missing Master Job Engine database tables")
            return {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                    for name in _TABLES}
    except sqlite3.Error as exc:
        raise RecoveryError("Invalid or corrupt SQLite backup") from exc


def _manifest_path(backup: Path) -> Path:
    return backup.with_name(backup.name + ".manifest.json")


def create_verified_backup(database: str | os.PathLike[str],
                           backup: str | os.PathLike[str], *,
                           signing_key: bytes | None = None,
                           require_signed: bool = True) -> dict[str, Any]:
    """Snapshot WAL-consistently with SQLite's online-backup API.

    Destination must not exist. The manifest is published after an integrity
    check and byte digest. Any failure removes newly created files, not source.
    """
    _key(signing_key, require_signed=require_signed)
    source = _file(database)
    output = _file(backup, required=False)
    manifest_path = _manifest_path(output)
    if source == output or output.exists() or manifest_path.exists():
        raise RecoveryError("Backup target already exists or overlaps source")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise RecoveryError("Backup parent must already exist and be trusted")
    tmp = output.with_name("." + output.name + "." + secrets.token_hex(12) + ".tmp")
    tmp_manifest = manifest_path.with_name("." + manifest_path.name + "." + secrets.token_hex(12) + ".tmp")
    linked_backup = False
    linked_manifest = False
    try:
        _fresh_file(tmp)
        uri = "file:" + quote(str(source), safe="/") + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=30)) as from_db:
                with closing(sqlite3.connect(tmp, timeout=30)) as to_db:
                    from_db.backup(to_db, pages=256, sleep=0.05)
        except sqlite3.Error as exc:
            raise RecoveryError("SQLite online backup failed") from exc
        rows = _inspect_database(tmp)
        size, digest = _digest(tmp)
        payload = {"format": _FORMAT, "sha256": digest, "size_bytes": size,
                   "schema_version": 3, "row_counts": rows,
                   "created_at": datetime.now(timezone.utc).isoformat()}
        result = {**payload, "hmac_sha256": _mac(payload, signing_key) if signing_key else None}
        _fresh_file(tmp_manifest)
        with tmp_manifest.open("wb") as stream:
            stream.write(_canonical(result) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Hard links fail with EEXIST rather than overwriting another backup.
        os.link(tmp, output, follow_symlinks=False)
        linked_backup = True
        os.link(tmp_manifest, manifest_path, follow_symlinks=False)
        linked_manifest = True
        return result
    except Exception:
        # Only remove paths *we* successfully linked. Never unlink another
        # process's winning concurrent backup/manifest on a name collision.
        for target, ours in ((manifest_path, linked_manifest), (output, linked_backup)):
            if ours:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        for target in (tmp, tmp_manifest):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass


def verify_verified_backup(backup: str | os.PathLike[str], *,
                           signing_key: bytes | None = None,
                           require_signed: bool = True) -> dict[str, Any]:
    """Verify manifest, HMAC (when required), SHA-256 and SQLite consistency."""
    _key(signing_key, require_signed=require_signed)
    backup_path = _file(backup)
    manifest_path = _file(_manifest_path(backup_path))
    with manifest_path.open("rb") as stream:
        content = stream.read(_MAX_MANIFEST_BYTES + 1)
    if len(content) > _MAX_MANIFEST_BYTES:
        raise RecoveryError("Backup manifest too large")
    try:
        obj = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise RecoveryError("Invalid backup manifest") from exc
    if not isinstance(obj, dict) or set(obj) != {"format", "sha256", "size_bytes", "schema_version", "row_counts", "created_at", "hmac_sha256"}:
        raise RecoveryError("Malformed backup manifest")
    signature = obj.pop("hmac_sha256")
    if (obj["format"] != _FORMAT or obj["schema_version"] != 3 or
            type(obj["size_bytes"]) is not int or not 0 < obj["size_bytes"] <= _LIMIT or
            not isinstance(obj["sha256"], str) or len(obj["sha256"]) != 64 or
            not isinstance(obj["created_at"], str) or
            not isinstance(obj["row_counts"], dict) or
            set(obj["row_counts"]) != set(_TABLES) or
            any(type(v) is not int or v < 0 for v in obj["row_counts"].values())):
        raise RecoveryError("Invalid manifest properties")
    if signing_key:
        if not isinstance(signature, str) or not hmac.compare_digest(_mac(obj, signing_key), signature):
            raise RecoveryError("Incorrect backup signature")
    elif require_signed or signature is not None:
        raise RecoveryError("Signed backup cannot be verified without its key")
    size, digest = _digest(backup_path)
    if size != obj["size_bytes"] or not hmac.compare_digest(digest, obj["sha256"]):
        raise RecoveryError("Backup data no longer matches manifest")
    if _inspect_database(backup_path) != obj["row_counts"]:
        raise RecoveryError("Backup row counts do not match manifest")
    return {**obj, "hmac_sha256": signature}


def restore_verified_backup(backup: str | os.PathLike[str],
                            destination: str | os.PathLike[str], *,
                            signing_key: bytes | None = None,
                            require_signed: bool = True) -> dict[str, Any]:
    """Create-only disaster recovery; never replace an existing production DB."""
    trusted = verify_verified_backup(backup, signing_key=signing_key,
                                     require_signed=require_signed)
    source = _file(backup)
    target = _file(destination, required=False)
    if target.exists() or target == source or target == _manifest_path(source):
        raise RecoveryError("Restore destination must not exist")
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise RecoveryError("Restore parent must be an existing trusted directory")
    temp = target.with_name("." + target.name + "." + secrets.token_hex(12) + ".tmp")
    try:
        _fresh_file(temp)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as inp, temp.open("wb") as out:
            while chunk := inp.read(1024 * 1024):
                size += len(chunk)
                if size > _LIMIT:
                    raise RecoveryError("Restore source exceeds size limit")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        # Verify bytes again after the copy, closing source-swap race windows.
        if size != trusted["size_bytes"] or not hmac.compare_digest(digest.hexdigest(), trusted["sha256"]):
            raise RecoveryError("Source backup changed during restore")
        if _inspect_database(temp) != trusted["row_counts"]:
            raise RecoveryError("Restored backup failed integrity checks")
        os.link(temp, target, follow_symlinks=False)
        return {"restored": True, "size_bytes": size, "sha256": digest.hexdigest(),
                "row_counts": trusted["row_counts"]}
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """Offline operator CLI; uses an environment key, never command-line secrets."""
    import argparse
    parser = argparse.ArgumentParser(description="Verified Master Job Engine disaster recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("backup")
    create.add_argument("database")
    create.add_argument("backup")
    check = commands.add_parser("verify")
    check.add_argument("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup")
    restore.add_argument("destination")
    parser.add_argument("--allow-unsigned", action="store_true",
                        help="Only for offline/dev backups; production backups must be signed")
    args = parser.parse_args(argv)
    try:
        key_hex = os.environ.get("AI_CREATOR_BACKUP_KEY_HEX")
        if key_hex:
            try:
                key = bytes.fromhex(key_hex)
            except ValueError as exc:
                raise RecoveryError("Malformed backup key in environment") from exc
        else:
            key = None
        kwargs = {"signing_key": key, "require_signed": not args.allow_unsigned}
        if args.command == "backup":
            result = create_verified_backup(args.database, args.backup, **kwargs)
        elif args.command == "verify":
            result = verify_verified_backup(args.backup, **kwargs)
        else:
            result = restore_verified_backup(args.backup, args.destination, **kwargs)
        # Print only safe counts and success indicators, no job snapshots or key.
        print(json.dumps({"ok": True, "sha256": result["sha256"],
                          "row_counts": result["row_counts"]}, sort_keys=True))
        return 0
    except (RecoveryError, OSError, sqlite3.Error, ValueError):
        # Errors from untrusted filesystem paths may contain sensitive data.
        print("RecoveryError: operation was not verified or was refused", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
