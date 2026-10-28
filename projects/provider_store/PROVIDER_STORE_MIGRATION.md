# Provider Store Migration and Recovery Guide

## Before changing a catalog

1. Back up the source file and confirm it is a schema-valid catalog.
2. Prepare the candidate in a separate path; do not edit the live file in place.
3. Review a redacted diff:

    python -m projects.provider_store.catalog_cli diff projects/provider_store/provider_catalog.json path/to/candidate.json

4. Create a snapshot before a risky maintenance operation:

    python -m projects.provider_store.catalog_cli snapshot projects/provider_store/provider_catalog.json build/catalog-history
    python -m projects.provider_store.catalog_cli history build/catalog-history

## Apply a candidate

The default apply command is a dry run:

    python -m projects.provider_store.catalog_cli apply projects/provider_store/provider_catalog.json path/to/candidate.json

Review added/removed IDs and changed field names; field values are deliberately omitted. To commit the candidate, pass --apply. If any provider is removed, the operation also requires --allow-removals. Invalid schemas or failed atomic replacement must leave the original catalog intact.

## Import/export

Use catalog_cli export SOURCE DESTINATION to export validated metadata. Use catalog_cli import SOURCE DESTINATION to import a schema-valid catalog; a destination backup is created by default. The Python API in catalog_interchange.py uses a destination lock, same-directory temporary file, atomic replacement and post-write verification. A failed import restores the original bytes where possible and reports a safe failure status.

## Versioned configuration migration

provider_config_lifecycle.py validates the current and target schema versions, applies an explicitly registered migration, validates the candidate and writes it through a temporary file with a backup. On write failure it preserves the original file and cleans the temporary file. Rollback validates the prior configuration before changing history; an invalid prior version must not be discarded.

## Restore a snapshot

    python -m projects.provider_store.catalog_cli restore build/catalog-history/NAME.json projects/provider_store/provider_catalog.json

Only restore a trusted snapshot whose schema and provider metadata are valid. The destination import path creates a backup and validates the replacement. Catalog restoration does not restore credentials, OAuth sessions, provider connections, health, job queues, credits, quotas, billing ledgers or external provider state.

## Snapshot retention

Preview first:

    python -m projects.provider_store.catalog_cli prune build/catalog-history --keep 20

Only after reviewing the names in would_delete, pass --apply. Invalid snapshots and symlinks are not valid retention candidates. Keep an independent backup for critical environments.

## Recovery limitations

Local atomic file replacement and OS locks protect cooperating local writers; they do not provide a transaction across multiple manager files or devices. For production multi-worker coordination, prefer a transactional database and an explicit reconciliation workflow. Test recovery on a copy before operating on production data.
