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

from projects.provider_store.encrypted_state_store import EncryptedJSONStateStore, EncryptedStateError
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
        encryption_key_provider: Any = None,
        encryption_context: str = "generation-assets-v1",
        require_encrypted_metadata: bool = False,
        migrate_legacy_plaintext_metadata: bool = False,
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
        if not isinstance(require_encrypted_metadata, bool) or not isinstance(migrate_legacy_plaintext_metadata, bool):
            raise GenerationAssetPrivacyError("metadata encryption flags must be boolean")
        if encryption_key_provider is not None and not callable(encryption_key_provider):
            raise GenerationAssetPrivacyError("metadata encryption key provider must be callable")
        if (require_encrypted_metadata or migrate_legacy_plaintext_metadata) and encryption_key_provider is None:
            raise GenerationAssetPrivacyError("metadata encryption requires a deployment-managed key provider")
        if (
            not isinstance(encryption_context, str)
            or not 1 <= len(encryption_context) <= 100
            or any(ord(char) < 33 or ord(char) > 126 for char in encryption_context)
        ):
            raise GenerationAssetPrivacyError("metadata encryption context is invalid")
        self.max_document_bytes = max_document_bytes
        self.max_records = max_records
        self.encryption_key_provider = encryption_key_provider
        self.encryption_context = encryption_context
        self.require_encrypted_metadata = require_encrypted_metadata
        self.migrate_legacy_plaintext_metadata = migrate_legacy_plaintext_metadata

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

        self._metadata_stores: dict[str, EncryptedJSONStateStore] = {}
        if self.encryption_key_provider is not None:
            contexts = [self.encryption_context + ":manifest"] + [
                self.encryption_context + ":state:" + str(index)
                for index in range(len(self.state_paths))
            ]
            for path, context in zip(paths, contexts):
                if len(context) > 128:
                    raise GenerationAssetPrivacyError("metadata encryption context is too long")
                store = EncryptedJSONStateStore(
                    path,
                    key_provider=self.encryption_key_provider,
                    context=context,
                    migrate_legacy_plaintext=self.migrate_legacy_plaintext_metadata,
                    max_state_bytes=self.max_document_bytes,
                )
                if self.require_encrypted_metadata and not store.verify_key():
                    raise GenerationAssetPrivacyError("generation metadata encryption key is unavailable")
                self._metadata_stores[str(path.resolve(strict=False))] = store

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

    def audit(self) -> dict[str, Any]:
        """Return privacy readiness counts without exposing identifiers or paths."""
        lock_path = self.manifest_path.with_name(self.manifest_path.name + ".privacy.lock")
        if lock_path.is_symlink():
            return {"status": "ASSET_PRIVACY_AUDIT_FAILED", "document_count": 0, "record_count": 0, "scoped_count": 0, "unscoped_count": 0, "retention_ready_count": 0, "retention_missing_expiry_count": 0}
        try:
            with interprocess_file_lock(lock_path):
                specs = [(self.manifest_path, "assets")] + [(path, "states") for path in self.state_paths]
                record_count = 0
                scoped_count = 0
                unscoped_count = 0
                untracked_planned_count = 0
                retention_ready = 0
                retention_missing = 0
                encrypted_document_count = 0
                encryption_migration_count = 0
                for path, collection in specs:
                    document, rows = self._read_document(path, collection)
                    record_count += len(rows)
                    store = self._metadata_stores.get(str(path.resolve(strict=False)))
                    if store is not None:
                        if store.migration_required:
                            encryption_migration_count += 1
                        else:
                            encrypted_document_count += 1
                    for row in rows:
                        try:
                            self._scope(row, document)
                            actual_field = "file_path" if collection == "assets" else "output_file"
                            actual_paths = set()
                            if actual_field in row:
                                actual = self._resolve_asset_path(row[actual_field])
                                if actual is not None:
                                    actual_paths.add(actual)
                            if "planned_output_path" in row:
                                planned = self._resolve_asset_path(row["planned_output_path"])
                                if planned is not None and planned.exists() and planned not in actual_paths:
                                    untracked_planned_count += 1
                            scoped_count += 1
                            expiry = row.get("expires_at", document.get("expires_at", _MISSING))
                            if expiry is _MISSING:
                                retention_missing += 1
                            else:
                                self._aware_utc(expiry)
                                retention_ready += 1
                        except Exception:
                            unscoped_count += 1
                ready = unscoped_count == 0 and untracked_planned_count == 0
                status = (
                    "ASSET_PRIVACY_ENCRYPTION_MIGRATION_REQUIRED"
                    if self.require_encrypted_metadata and encryption_migration_count > 0
                    else "ASSET_PRIVACY_READY" if ready else "ASSET_PRIVACY_MIGRATION_REQUIRED"
                )
                return {
                    "status": status,
                    "document_count": len(specs),
                    "record_count": record_count,
                    "scoped_count": scoped_count,
                    "unscoped_count": unscoped_count,
                    "untracked_planned_count": untracked_planned_count,
                    "retention_ready_count": retention_ready,
                    "retention_missing_expiry_count": retention_missing,
                    "encryption_required": self.require_encrypted_metadata,
                    "encrypted_document_count": encrypted_document_count,
                    "encryption_migration_count": encryption_migration_count,
                }
        except Exception:
            return {"status": "ASSET_PRIVACY_AUDIT_FAILED", "document_count": 0, "record_count": 0, "scoped_count": 0, "unscoped_count": 0, "retention_ready_count": 0, "retention_missing_expiry_count": 0}

    def _read_document(self, path: Path, collection: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise GenerationAssetPrivacyError("configured asset privacy document is unavailable")
        size = path.stat().st_size
        if size < 1 or size > self.max_document_bytes:
            raise GenerationAssetPrivacyError("asset privacy document size is outside limits")
        try:
            store = self._metadata_stores.get(str(path.resolve(strict=False)))
            if store is not None:
                document = store.read({collection: []})
                if store.migration_required and not self.migrate_legacy_plaintext_metadata:
                    raise GenerationAssetPrivacyError("plaintext generation metadata requires explicit migration")
            else:
                raw = path.read_bytes()
                if len(raw) != size:
                    raise GenerationAssetPrivacyError("asset privacy document changed while reading")
                document = json.loads(raw.decode("utf-8"))
        except (EncryptedStateError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise GenerationAssetPrivacyError("asset privacy document is malformed or encrypted metadata is unavailable") from None
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

    def _assert_safe_asset_file(self, asset_path: Path) -> None:
        for root in self.asset_roots.values():
            try:
                relative = asset_path.relative_to(root)
            except ValueError:
                continue
            if root.is_symlink():
                raise GenerationAssetPrivacyError("asset root became a symlink")
            current = root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise GenerationAssetPrivacyError("asset path changed to a symlink")
            if current.exists() and not current.is_file():
                raise GenerationAssetPrivacyError("asset path is no longer a regular file")
            return
        raise GenerationAssetPrivacyError("asset path escaped the configured roots")

    def _atomic_write(self, path: Path, document: dict[str, Any]) -> None:
        store = self._metadata_stores.get(str(path.resolve(strict=False)))
        if store is not None:
            try:
                store.write(document)
                return
            except EncryptedStateError:
                raise GenerationAssetPrivacyError("encrypted asset metadata persistence failed") from None
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

    def migrate_legacy_metadata(self, *, confirmed: bool = False) -> dict[str, Any]:
        """Explicitly encrypt legacy manifest/state documents using dedicated KMS contexts."""
        if confirmed is not True:
            return {"status": "GENERATION_METADATA_MIGRATION_CONFIRMATION_REQUIRED", "migrated_document_count": 0}
        if not self.require_encrypted_metadata or not self.migrate_legacy_plaintext_metadata:
            return {"status": "GENERATION_METADATA_MIGRATION_NOT_CONFIGURED", "migrated_document_count": 0}
        lock_path = self.manifest_path.with_name(self.manifest_path.name + ".privacy.lock")
        if lock_path.is_symlink():
            return {"status": "GENERATION_METADATA_MIGRATION_FAILED", "migrated_document_count": 0}
        specs = [(self.manifest_path, "assets")] + [(path, "states") for path in self.state_paths]
        migrated = 0
        try:
            with interprocess_file_lock(lock_path):
                for path, collection in specs:
                    document, _rows = self._read_document(path, collection)
                    store = self._metadata_stores[str(path.resolve(strict=False))]
                    if store.migration_required:
                        store.write(document)
                        migrated += 1
            return {
                "status": "GENERATION_METADATA_ENCRYPTED" if migrated else "GENERATION_METADATA_ALREADY_ENCRYPTED",
                "migrated_document_count": migrated,
                "document_count": len(specs),
            }
        except Exception:
            return {
                "status": "GENERATION_METADATA_MIGRATION_PARTIAL" if migrated else "GENERATION_METADATA_MIGRATION_FAILED",
                "migrated_document_count": migrated,
                "document_count": len(specs),
            }

    def migrate_legacy_scope_metadata(
        self,
        *,
        scope_resolver: Any,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Resolve legacy ownership using a trusted deployment source, never inference.

        The callback receives only non-personal asset/job metadata and must return
        explicit project_id, tenant_id (including explicit None) and pseudonymous
        subject_ref. All documents are preflighted before any manifest is rewritten.
        """
        if confirmed is not True:
            return {"status": "GENERATION_SCOPE_MIGRATION_CONFIRMATION_REQUIRED", "migrated_record_count": 0}
        if not callable(scope_resolver):
            return {"status": "GENERATION_SCOPE_RESOLVER_UNAVAILABLE", "migrated_record_count": 0}
        lock_path = self.manifest_path.with_name(self.manifest_path.name + ".privacy.lock")
        if lock_path.is_symlink():
            return {"status": "GENERATION_SCOPE_MIGRATION_FAILED", "migrated_record_count": 0}
        specs = [(self.manifest_path, "assets")] + [(path, "states") for path in self.state_paths]
        prepared: list[tuple[Path, dict[str, Any], int]] = []
        migrated_records = 0
        written_records = 0
        try:
            with interprocess_file_lock(lock_path):
                for path, collection in specs:
                    document, rows = self._read_document(path, collection)
                    rewritten = []
                    changed = 0
                    for original in rows:
                        row = dict(original)
                        try:
                            self._scope(row, document)
                        except GenerationAssetPrivacyError:
                            lookup = {
                                key: row[key]
                                for key in ("asset_id", "job_id", "project", "scene", "media_type")
                                if key in row
                            }
                            try:
                                resolved = scope_resolver(lookup)
                            except Exception as exc:
                                raise GenerationAssetPrivacyError("authoritative asset scope lookup failed") from exc
                            if not isinstance(resolved, Mapping):
                                raise GenerationAssetPrivacyError("authoritative asset scope response is invalid")
                            validated = copy_generation_privacy_scope({}, resolved)
                            for key in ("project_id", "tenant_id", "subject_ref"):
                                row[key] = validated[key]
                            if "expires_at" in validated:
                                row["expires_at"] = validated["expires_at"]
                            self._scope(row, document)
                            changed += 1
                        rewritten.append(row)
                    updated = dict(document)
                    updated[collection] = rewritten
                    prepared.append((path, updated, changed))
                    migrated_records += changed

                # Do not write anything until every legacy row passed the authoritative preflight.
                for path, document, changed in prepared:
                    if changed:
                        self._atomic_write(path, document)
                        written_records += changed
            return {
                "status": "GENERATION_SCOPE_METADATA_MIGRATED" if migrated_records else "GENERATION_SCOPE_METADATA_ALREADY_VALID",
                "migrated_record_count": migrated_records,
                "document_count": len(specs),
            }
        except Exception:
            return {
                "status": "GENERATION_SCOPE_MIGRATION_PARTIAL" if written_records else "GENERATION_SCOPE_MIGRATION_FAILED",
                "migrated_record_count": written_records,
                "document_count": len(specs),
            }

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

            docs: list[tuple[Path, str, tuple[str, ...], tuple[str, ...]]] = [
                (self.manifest_path, "assets", ("file_path",), ("planned_output_path",)),
            ]
            docs.extend((path, "states", ("output_file",), ("planned_output_path",)) for path in self.state_paths)
            loaded: list[dict[str, Any]] = []
            target_paths: set[Path] = set()
            other_paths: set[Path] = set()
            changed_counts: list[int] = []

            for path, collection, actual_fields, planned_fields in docs:
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
                    actual_paths: list[Path] = []
                    for field in actual_fields:
                        if field in row:
                            candidate = self._resolve_asset_path(row[field])
                            if candidate is not None:
                                actual_paths.append(candidate)
                    planned_paths: list[Path] = []
                    for field in planned_fields:
                        if field in row:
                            candidate = self._resolve_asset_path(row[field])
                            if candidate is not None and candidate.exists():
                                planned_paths.append(candidate)
                    if selected:
                        # If a planned file exists but no matching actual output
                        # reference records it, do not claim complete erasure.
                        if any(candidate not in actual_paths for candidate in planned_paths):
                            raise GenerationAssetPrivacyError("an untracked planned output file exists")
                        target_paths.update(actual_paths)
                        removed += 1
                    else:
                        other_paths.update(actual_paths)
                        other_paths.update(planned_paths)
                        kept.append(row)
                document[collection] = kept
                loaded.append({"path": path, "document": document, "changed": removed > 0})
                changed_counts.append(removed)

            if target_paths.intersection(other_paths):
                raise GenerationAssetPrivacyError("an asset file is shared with a non-target privacy record")

            # Delete physical assets first. If a later metadata write fails, the
            # old record remains retryable and an already-absent file is harmless.
            for asset_path in sorted(target_paths, key=str):
                self._assert_safe_asset_file(asset_path)
                if asset_path.exists():
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
            if lock_path.is_symlink():
                raise GenerationAssetPrivacyError("asset privacy lock symlink is not accepted")
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
            if lock_path.is_symlink():
                raise GenerationAssetPrivacyError("asset privacy lock symlink is not accepted")
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
