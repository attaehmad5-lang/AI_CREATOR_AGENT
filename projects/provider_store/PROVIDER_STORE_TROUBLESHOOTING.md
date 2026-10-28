# Provider Store Troubleshooting

## Readiness reports DEGRADED

Run from the repository root:

    python -m projects.provider_store.advanced.advanced_provider_manager

Review the structured blockers. A catalog entry, enabled flag or configured credential alone does not establish a live connection. Configure only the intended provider and run an authorized, non-billable verification endpoint where the provider documents one.

## Catalog JSON is invalid or inconsistent

- Do not manually overwrite a valid catalog with an unreviewed candidate.
- Validate the candidate and compare metadata first:

    python -m projects.provider_store.catalog_cli diff projects/provider_store/provider_catalog.json path/to/candidate.json
    python -m projects.provider_store.catalog_cli apply projects/provider_store/provider_catalog.json path/to/candidate.json

- apply previews by default. Use --apply only after reviewing the redacted diff. Removing providers additionally requires --allow-removals.
- Import/export operations validate the schema; import takes a lock and creates a .bak backup by default.
- If the active catalog is missing or invalid, use ConnectionManager.restore_catalog_from_master(). It refuses to overwrite an already-valid catalog.

## Snapshot history or restore

Create a snapshot with catalog_cli snapshot SOURCE HISTORY_DIRECTORY; list names/counts with catalog_cli history HISTORY_DIRECTORY. Restore only a trusted, schema-valid snapshot with catalog_cli restore SNAPSHOT DESTINATION. Retention pruning is preview-only until --apply is passed; verify the preview and keep count before deletion.

## Connectivity/authentication failures

- Confirm the endpoint is HTTPS except for explicitly permitted loopback test endpoints, and that its hostname is allowlisted.
- Confirm the configured credential environment-variable name exists; never paste the secret into logs or issue reports.
- Set authentication_required=True only when official endpoint documentation confirms that the selected probe endpoint validates the credential.
- Redirects are rejected. Do not disable TLS verification or broaden allowlists to silence a failure.
- A timeout or HTTP 2xx does not prove that generation, subscription, or account balance works. Use a documented provider-specific check.

## Paid-use or balance routing is blocked

Paid execution requires explicit approval and a configured budget. Credit snapshots must be verified, fresh and unit-compatible with the requested route. Do not translate provider credits into USD without a documented conversion. Never release a reservation merely because a started request failed; it may still have consumed quota or money.

## Output validation rejects a file

Check the reported modality, extension, size and basic signature. The validator does not perform full media decoding or semantic quality assessment. Do not bypass the check to accept malformed or mismatched artifacts.

## Release report or ZIP fails

Run:

    python -m projects.provider_store.advanced.release_report --output-dir build/advanced-provider-store-release

The report should remain INCOMPLETE until every ledger point is completed. A package-generation error should be treated as a release blocker; do not remove the source allowlist or secret scan to force success. Inspect the error label/path only—never print credential values.

## CI failure triage

- **Ruff format/lint:** apply the pinned Ruff version to the exact modules listed in the workflow and commit the resulting changes.
- **mypy:** resolve type errors in the focused checked modules; do not suppress an entire module to get a green run.
- **coverage:** add branch/failure tests for changed safety-critical paths.
- **wheel/package:** reproduce from a clean checkout and verify package data, manifest hashes, ZIP CRC and SBOM membership.
- **live integration:** use authorized credentials and an explicitly approved test budget; do not put secrets into CI logs or source fixtures.
