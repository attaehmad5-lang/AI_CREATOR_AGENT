# Provider Store API Reference

This reference covers the supported operational entry points and the key policy/lifecycle modules. Internal helpers and private attributes (names beginning with an underscore) are not public APIs. Consult function/class docstrings in source for exact signatures.

## Command-line entry points

### Advanced readiness

    python -m projects.provider_store.advanced.advanced_provider_manager

Prints a secret-free readiness report and actionable blockers. A DEGRADED result is valid when providers lack intentionally configured credentials or live verification.

### Catalog maintenance

    python -m projects.provider_store.catalog_cli diff CURRENT CANDIDATE
    python -m projects.provider_store.catalog_cli apply CURRENT CANDIDATE
    python -m projects.provider_store.catalog_cli apply CURRENT CANDIDATE --apply
    python -m projects.provider_store.catalog_cli apply CURRENT CANDIDATE --apply --allow-removals
    python -m projects.provider_store.catalog_cli export SOURCE DESTINATION
    python -m projects.provider_store.catalog_cli import SOURCE DESTINATION
    python -m projects.provider_store.catalog_cli snapshot SOURCE HISTORY_DIRECTORY
    python -m projects.provider_store.catalog_cli history HISTORY_DIRECTORY
    python -m projects.provider_store.catalog_cli restore SNAPSHOT DESTINATION
    python -m projects.provider_store.catalog_cli prune HISTORY_DIRECTORY --keep 20
    python -m projects.provider_store.catalog_cli prune HISTORY_DIRECTORY --keep 20 --apply

Mutation is opt-in. Apply defaults to dry-run, removals require explicit approval, and pruning previews deletions until --apply is passed.

### Release report

    python -m projects.provider_store.advanced.release_report --output-dir build/advanced-provider-store-release
    python -m projects.provider_store.advanced.release_report --output-dir build/advanced-provider-store-release --require-complete

The ordinary report command can generate an INCOMPLETE report for review. The strict flag exits nonzero unless all 150 acceptance points are COMPLETED. Generated artifacts include ledger-derived JSON/Markdown/PDF reports, a source manifest, SPDX 2.3 source SBOM, a source ZIP and a SHA-256 sidecar.

## Catalog APIs

### projects.provider_store.catalog_interchange

- export_catalog(source, destination) — read and validate a catalog, then write it through a same-directory temporary file and atomic replacement. Returns a status and provider count.
- import_catalog(source, destination, backup=True) — validate the source, take the destination interprocess lock, optionally preserve prior bytes in a .bak file, replace atomically and verify the result. A failed write/verification attempts to restore the original bytes.
- create_catalog_snapshot(source, history_directory) — create an immutable timestamped snapshot of a valid catalog.
- list_catalog_snapshots(history_directory) — list safe snapshot metadata without exposing provider configuration values.
- restore_catalog_snapshot(snapshot, destination) — validate and restore a named snapshot through the import/backup path.
- prune_catalog_snapshots(history_directory, keep=20, apply=False) — preview retention by default; deletion requires explicit apply.

### projects.provider_store.catalog_diff

- diff_catalogs(current, candidate) — compare validated catalogs and return redacted change metadata.
- apply_catalog_candidate(current, candidate, dry_run=True, allow_removals=False) — preview by default; mutation requires dry_run=False, and removal requires explicit approval.

### projects.provider_store.connection_manager

Use the canonical manager for provider registration, catalog validation, enable/disable state, adapter contract enforcement, connection-state tracking and validated catalog recovery. The advanced manager is a compatibility facade over the canonical registry, not a second independent source of truth.

### projects.provider_store.provider_config_lifecycle

Use the configuration lifecycle manager for version-validated candidate reload, explicitly registered schema migrations and rollback to a validated prior configuration. A failed migration write must preserve the original bytes and clean up its temporary file.

Use migrate_file(..., dry_run=True) to preview the validated target configuration and migration step count without writing the source file, creating a backup or changing the active generation. The default remains mutating for backwards compatibility; pass dry_run=True explicitly for preview behavior.

## Routing simulation

The route planner supports an explicit side-effect-free preview:

    preview = router.route(
        category="image",
        project_id="demo",
        request_id="preview-001",
        estimated_cost=2.50,
        paid_approved_providers={"provider-id"},
        dry_run=True,
    )

A successful preview returns status ROUTE_SIMULATED, the selected provider, policy decisions, estimated cost/unit, a would_reserve list, and a policy_matrix containing eligible provider IDs, rejected-provider decisions, the selected provider, fallback state and reservation plan. Project policies may constrain allowed/denied providers, models, capabilities, maximum estimated cost, health/verified-connection evidence, automatic fallback and required data-residency region. Region identifiers are validated and a provider without matching, declared region metadata is rejected. A preview does not reserve paid budget, free credits or quota, and does not advance the round-robin cursor. A preview does not guarantee that quota or balances will still be available when a real request is submitted. Omit dry_run (or set it to False) only when the caller intends normal routing and reservation behavior.

## Pricing catalog and estimates

The pricing catalog is a source-attributed metadata store with atomic, validated price-record updates. Each price record identifies an exact provider, model, modality, unit, currency, effective date range, source and verification timestamp. Estimates fail closed when no unique applicable price exists.

    catalog = PricingCatalog("projects/provider_store/pricing_catalog.json")
    estimate = catalog.estimate(
        provider_id="provider-id",
        model_id="model-id",
        modality="image",
        units=4,
        currency="USD",
    )
    decision = cost_policy.evaluate_priced_request(
        provider_metadata,
        pricing_catalog=catalog,
        model_id="model-id",
        modality="image",
        units=4,
        currency="USD",
        project_id="demo",
        paid_usage_approved=False,
    )

Use catalog.upsert_price(record) to add or replace one fully validated price record. The update is serialized across processes and written through an atomic replacement; malformed records, symlinked catalogs and failed writes are rejected without claiming success. The checked-in pricing_catalog.json is intentionally empty until official vendor prices have been verified.

A returned estimate is not an invoice, live quote, account balance or guarantee of provider billing. evaluate_priced_request passes the estimate into the existing project-budget and explicit paid-approval gate. Do not populate the catalog with guessed prices; use provider-published sources and record when they were verified.

## Runtime and policy modules

| Module | Responsibility |
|---|---|
| provider_service.py | Coordinates verification, health recording, routing, output checks and usage finalization |
| http_probe.py | Bounded allowlisted HTTPS probes with TLS verification, redirects rejected, and safe result metadata |
| oauth_pkce_manager.py | OAuth authorization-code PKCE, expiring state and in-memory token handling |
| routing_manager.py | Policy-aware provider/model selection, fallback and reservation checks |
| cost_policy.py | Paid approval, budget reservations, pricing-estimate integration and idempotent usage reconciliation |
| pricing_catalog.py | Atomic source-attributed provider/model pricing updates and date-aware unit cost estimates |\n| credit_manager.py | Verified credit snapshots and local reserve/commit/release |
| quota_manager.py | UTC quota windows and request reservation lifecycle |
| provider_job_manager.py | Bounded asynchronous job queue, ownership, polling, cancellation and recovery |
| health_manager.py | Health history, active probes, eligibility and recovery |
| health_scheduler.py | Poll-driven bounded health scheduling; the application owns the polling loop |
| provider_outage_detector.py | Deduplicated outage incidents and recovery resolution |
| provider_structured_logger.py | Bounded secret-safe structured events |
| provider_trace_manager.py | Bounded W3C trace-context propagation and secret-safe spans |
| output_validator.py | Bounded artifact checks, basic signature/content checks and SHA-256 |
| media_integrity_validator.py | Modality-specific structural integrity checks |
| provider_error_policy.py | Normalized safe provider error outcomes |
| provider_webhook_verifier.py | Bounded HMAC signature, timestamp and replay verification |

## Safety and compatibility notes

- Keep secret values in environment/in-memory interfaces; store only references in catalogs.
- A configured credential is not the same as verified connectivity.
- Generic HTTP success does not prove generation, subscription or credit balance.
- Paid execution requires explicit approval and a configured budget.
- A failed request may still consume quota or cost; reconcile ambiguous outcomes conservatively.
- Local file locks do not create a transaction across separate manager files or devices.
- Output signature checks do not replace full media decoding or semantic quality checks.
- Treat status strings and result dictionaries as explicit outcomes; do not convert a failure result into success merely because a command exited without an exception.
