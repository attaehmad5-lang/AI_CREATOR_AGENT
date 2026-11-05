"""Fail-closed privacy deletion for generated media manifests and local assets.

The adapter is deliberately opt-in. Every manifest row must carry a
pseudonymous subject_ref, project_id and an explicit tenant_id (which may be
None). Legacy unscoped rows block confirmation until they are migrated.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from projects.provider_store.interprocess_lock import (
    atomic_replace_with_retry,
    interprocess_file_lock,
)

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MISSING = object()


class GenerationAssetPrivacyError(ValueError):
    """Raised internally when asset privacy metadata or paths are unsafe."""


def copy_generation_privacy_scope(
    document: Mapping[str, Any],
    record: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return validated, explicit scope metadata for one manifest/state row.

    Row values override document defaults. If any of project_id, tenant_id or
    subject_ref is supplied, all three must be explicit (tenant_id may be None).
    An entirely unscoped legacy row remains unmodified and is later rejected by
    the deletion adapter; the helper never guesses an owner.
    """
    if not isinstance(document, Mapping) or (record is not None and not isinstance(record, Mapping)):
        raise GenerationAssetPrivacyError("generation privacy scope source must be a mapping")
    row = record or {}
    names = ("project_id", "tenant_id", "subject_ref")
    present = any(name in row or name in document for name in names)
    if not present:
        scope: dict[str, Any] = {}
    else:
        scope = {
            name: row[name] if name in row else document.get(name, _MISSING)
            for name in names
        }
        if any(value is _MISSING for value in scope.values()):
            raise GenerationAssetPrivacyError("generation privacy scope must define project, tenant and subject")
        if not isinstance(scope["project_id"], str) or _REFERENCE.fullmatch(scope["project_id"]) is None:
            raise GenerationAssetPrivacyError("generation project ID is invalid")
        if scope["tenant_id"] is not None and (
            not isinstance(scope["tenant_id"], str) or _REFERENCE.fullmatch(scope["tenant_id"]) is None
        ):
            raise GenerationAssetPrivacyError("generation tenant ID is invalid")
        if not GenerationAssetPrivacyAdapter._valid_subject(scope["subject_ref"]):
            raise GenerationAssetPrivacyError("generation subject reference must be pseudonymous")

    expiry = row["expires_at"] if "expires_at" in row else document.get("expires_at", _MISSING)
    if expiry is not _MISSING:
        GenerationAssetPrivacyAdapter._aware_utc(expiry)
        scope["expires_at"] = expiry
    return scope


class GenerationAssetPrivacyAdapter:
    """Delete/purge generated asset records and files under explicit roots.

    asset_roots maps manifest-relative prefixes to dedicated directories,
    e.g. {"output/images": "/srv/creator/output/images"}. Only file_path
    and state output_file values are physical deletion candidates.
    planned_output_path is never deleted. An OS-level interprocess lock and
    atomic JSON replacement protect manifests from concurrent writers.
    """

    def __init__(
        self,
        manifest_path: str | os.PathLike[str],
        *,
        asset_roots: Mapping[str, str | os.PathLike[str]],
        state_paths: Sequence[str | os.PathLike[str]] = (),
        max_document_bytes: int = 16 * 1024 * 1024,
        max_records: int = 100_000,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.state_paths = tuple(Path(path) for path in state_paths)
        if len(self.state_paths) > 16:
            raise GenerationAssetPrivacyError("too many generation state documents")
        if not isinstance(asset_roots, Mapping) or not asset_roots:
            raise GenerationAssetPrivacyError("asset root mapping is required")
        if isinstance(max_document_bytes, bool) or not isinstance(max_document_bytes, int) or not 1024 <= max_document_bytes <= 256 * 1024 * 1024:
            raise GenerationAssetPrivacyError("document size limit is invalid")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or not 1 <= max_records <= 1_000_000:
            raise GenerationAssetPrivacyError("record limit is invalid")
        if self.manifest_path.is_symlink():
            raise GenerationAssetPrivacyError("manifest symlinks are not accepted")
        self.max_document_bytes = max_document_bytes
        self.max_records = max_records

        roots: dict[str, Path] = {}
        for prefix, directory in asset_roots.items():
            if not isinstance(prefix, str) or not prefix or len(prefix) > 256:
                raise GenerationAssetPrivacyError("asset root prefix is invalid")
            prefix_path = PurePosixPath(prefix.replace("\\", "/"))
            if prefix_path.is_absolute() or any(part in {"", ".", ".."} for part in prefix_path.parts):
                raise GenerationAssetPrivacyError("asset root prefix is unsafe")
            if not isinstance(directory, (str, os.PathLike)):
                raise GenerationAssetPrivacyError("asset root directory is invalid")
            root = Path(directory)
            if root.is_symlink() or not root.exists() or not root.is_dir():
                raise GenerationAssetPrivacyError("asset root must be an existing real directory")
            resolved = root.resolve(strict=True)
            normalized_prefix = prefix_path.as_posix().rstrip("/")
            if normalized_prefix in roots:
                raise GenerationAssetPrivacyError("duplicate asset root prefix")
            roots[normalized_prefix] = resolved
        self.asset_roots = roots

        paths = (self.manifest_path, *self.state_paths)
        normalized = [str(path.resolve(strict=False)) for path in paths]
        if len(set(normalized)) != len(normalized):
            raise GenerationAssetPrivacyError("manifest and state documents must be distinct")

    @staticmethod
    def _valid_subject(value: Any) -> bool:
        return (
            isinstance(value, str)
            and _SUBJECT.fullmatch(value) is not None
            and any(char.isalpha() for char in value)
            and "@" not in value
        )

    @staticmethod
    def _aware_utc(value: Any) -> datetime:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise GenerationAssetPrivacyError("expiry metadata is missing or invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise GenerationAssetPrivacyError("expiry metadata is invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise GenerationAssetPrivacyError("expiry metadata must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _scope(record: Mapping[str, Any], document: Mapping[str, Any]) -> tuple[str, str | None, str]:
        values: dict[str, Any] = {}
        for key in ("project_id", "tenant_id", "subject_ref"):
            value = record[key] if key in record else document.get(key, _MISSING)
            if value is _MISSING:
                raise GenerationAssetPrivacyError("legacy asset record lacks explicit privacy scope")
            values[key] = value
        project_id = values["project_id"]
        tenant_id = values["tenant_id"]
        subject_ref = values["subject_ref"]
        if not isinstance(project_id, str) or _REFERENCE.fullmatch(project_id) is None:
            raise GenerationAssetPrivacyError("asset project scope is invalid")
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or _REFERENCE.fullmatch(tenant_id) is None
        ):
            raise GenerationAssetPrivacyError("asset tenant scope is invalid")
        if not GenerationAssetPrivacyAdapter._valid_subject(subject_ref):
            raise GenerationAssetPrivacyError("asset subject handle is not pseudonymous")
        return project_id, tenant_id, subject_ref

    def _read_document(self, path: Path, collection: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise GenerationAssetPrivacyError("configured asset privacy document is unavailable")
        size = path.stat().st_size
        if size < 1 or size > self.max_document_bytes:
            raise GenerationAssetPrivacyError("asset privacy document size is outside limits")
        raw = path.read_bytes()
        if len(raw) != size:
            raise GenerationAssetPrivacyError("asset privacy document changed while reading")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise GenerationAssetPrivacyError("asset privacy document is malformed") from None
        if not isinstance(document, dict) or not isinstance(document.get(collection), list):
            raise GenerationAssetPrivacyError("asset privacy document schema is invalid")
        records = document[collection]
        if len(records) > self.max_records or any(not isinstance(row, dict) for row in records):
            raise GenerationAssetPrivacyError("asset privacy record collection is invalid")
        return document, records

    def _resolve_asset_path(self, value: Any) -> Path | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise GenerationAssetPrivacyError("asset path is invalid")
        normalized = value.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            relative.is_absolute()
            or re.match(r"^[A-Za-z]:", normalized)
            or any(part in {"", ".", ".."} for part in relative.parts)
            or any(ord(char) < 32 for char in normalized)
        ):
            raise GenerationAssetPrivacyError("asset path is not a safe relative path")

        parts = relative.parts
        for prefix, root in self.asset_roots.items():
            prefix_parts = PurePosixPath(prefix).parts
            if tuple(parts[:len(prefix_parts)]) != tuple(prefix_parts) or len(parts) <= len(prefix_parts):
                continue
            candidate = root
            suffix = parts[len(prefix_parts):]
            for index, part in enumerate(suffix):
                candidate = candidate / part
                if candidate.is_symlink():
                    raise GenerationAssetPrivacyError("asset path contains a symlink")
                if index < len(suffix) - 1 and candidate.exists() and not candidate.is_dir():
                    raise GenerationAssetPrivacyError("asset path parent is not a directory")
            resolved = candidate.resolve(strict=False)
            if root != resolved and root not in resolved.parents:
                raise GenerationAssetPrivacyError("asset path escapes its configured root")
            if candidate.exists() and not candidate.is_file():
                raise GenerationAssetPrivacyError("asset path is not a regular file")
            return candidate
        raise GenerationAssetPrivacyError("asset path is outside configured roots")

    def _atomic_write(self, path: Path, document: dict[str, Any]) -> None:
        encoded = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(encoded) > self.max_document_bytes:
            raise GenerationAssetPrivacyError("updated asset privacy document exceeds size limit")
        descriptor, temporary = tempfile.mkstemp(
            prefix="." + path.name + ".privacy-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if path.is_symlink():
                raise GenerationAssetPrivacyError("asset privacy document became a symlink")
            atomic_replace_with_retry(temporary, path)
        except BaseException:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
            raise

    def _operate(
        self,
        *,
        operation: str,
        project_id: str,
        tenant_id: str | None,
        request_id: str,
        subject_ref: str | None = None,
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        success = "DELETED" if operation == "delete" else "PURGED"
        already = "ALREADY_ABSENT" if operation == "delete" else "ALREADY_EMPTY"
        try:
            if not isinstance(project_id, str) or _REFERENCE.fullmatch(project_id) is None:
                raise GenerationAssetPrivacyError("project scope is invalid")
            if tenant_id is not None and (
                not isinstance(tenant_id, str) or _REFERENCE.fullmatch(tenant_id) is None
            ):
                raise GenerationAssetPrivacyError("tenant scope is invalid")
            if not isinstance(request_id, str) or _REQUEST.fullmatch(request_id) is None:
                raise GenerationAssetPrivacyError("request identifier is invalid")
            if operation == "delete" and not self._valid_subject(subject_ref):
                raise GenerationAssetPrivacyError("subject scope is invalid")
            if operation == "purge" and (
                not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None
            ):
                raise GenerationAssetPrivacyError("retention cutoff must be timezone-aware")

            docs: list[tuple[Path, str, tuple[str, ...]]] = [
                (self.manifest_path, "assets", ("file_path",)),
            ]
            docs.extend((path, "states", ("output_file",)) for path in self.state_paths)
            loaded: list[dict[str, Any]] = []
            target_paths: set[Path] = set()
            other_paths: set[Path] = set()
            changed_counts: list[int] = []

            for path, collection, file_fields in docs:
                document, rows = self._read_document(path, collection)
                kept: list[dict[str, Any]] = []
                removed = 0
                for row in rows:
                    row_project, row_tenant, row_subject = self._scope(row, document)
                    in_scope = row_project == project_id and row_tenant == tenant_id
                    selected = in_scope and (
                        row_subject == subject_ref if operation == "delete" else False
                    )
                    if operation == "purge" and in_scope:
                        expiry_value = row.get("expires_at", document.get("expires_at", _MISSING))
                        if expiry_value is _MISSING:
                            raise GenerationAssetPrivacyError("scoped asset lacks an explicit expiry")
                        selected = self._aware_utc(expiry_value) <= cutoff.astimezone(timezone.utc)
                    resolved_paths: list[Path] = []
                    for field in file_fields:
                        if field in row:
                            candidate = self._resolve_asset_path(row[field])
                            if candidate is not None:
                                resolved_paths.append(candidate)
                    if selected:
                        target_paths.update(resolved_paths)
                        removed += 1
                    else:
                        other_paths.update(resolved_paths)
                        kept.append(row)
                document[collection] = kept
                loaded.append({"path": path, "document": document, "changed": removed > 0})
                changed_counts.append(removed)

            if target_paths.intersection(other_paths):
                raise GenerationAssetPrivacyError("an asset file is shared with a non-target privacy record")

            # Delete physical assets first. If a later metadata write fails, the
            # old record remains retryable and an already-absent file is harmless.
            for asset_path in sorted(target_paths, key=str):
                if asset_path.exists():
                    if asset_path.is_symlink() or not asset_path.is_file():
                        raise GenerationAssetPrivacyError("asset changed to a non-regular file")
                    asset_path.unlink()

            for entry in loaded:
                if entry["changed"]:
                    self._atomic_write(entry["path"], entry["document"])

            total = sum(changed_counts)
            return {
                "confirmed": True,
                "status": success if total else already,
                "deleted_count": total,
            }
        except Exception:
            return {"confirmed": False, "status": "FAILED", "deleted_count": 0}

    def delete_subject_data(
        self,
        *,
        subject_ref: str,
        project_id: str,
        tenant_id: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        lock_path = self.manifest_path.with_name(self.manifest_path.name + ".privacy.lock")
        try:
            with interprocess_file_lock(lock_path):
                return self._operate(
                    operation="delete",
                    subject_ref=subject_ref,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                )
        except Exception:
            return {"confirmed": False, "status": "FAILED", "deleted_count": 0}

    def purge_expired_data(
        self,
        *,
        cutoff: datetime,
        project_id: str,
        tenant_id: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        lock_path = self.manifest_path.with_name(self.manifest_path.name + ".privacy.lock")
        try:
            with interprocess_file_lock(lock_path):
                return self._operate(
                    operation="purge",
                    cutoff=cutoff,
                    project_id=project_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                )
        except Exception:
            return {"confirmed": False, "status": "FAILED", "deleted_count": 0}
