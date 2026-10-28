# Provider Store Architecture

## Scope

The Provider Store maintains provider metadata, connection evidence, routing policy and local execution-control state. The canonical registry is projects/provider_store/provider_catalog.json; the master catalog is a validated recovery copy. The advanced catalog is a compatibility/capability view, not independent evidence of runtime connectivity.

## Main layers

1. **Catalog and lifecycle** — connection_manager.py, catalog schemas, registry facades and advanced/advanced_provider_manager.py validate provider identity, enabled state and modality callable contracts.
2. **Authentication and connectivity** — http_probe.py provides bounded allowlisted HTTPS checks; oauth_pkce_manager.py implements in-memory PKCE state/token handling; provider_service.py coordinates verification. Credential presence is distinct from a verified connection.
3. **Policy and routing** — provider_data_policy.py, sensitive_data_policy.py, provider_authorization.py, routing_manager.py, cost_policy.py, credit_manager.py and quota_manager.py enforce permission, privacy, compatibility, verified-credit and budget/quota constraints.
4. **Execution and output** — provider_job_manager.py, retry_policy.py, provider_error_policy.py, output_validator.py and media_integrity_validator.py bound jobs/retries and validate returned artifacts.
5. **Operations and observability** — health_manager.py, health_scheduler.py, provider_outage_detector.py, provider_incident_manager.py, provider_metrics_manager.py, provider_structured_logger.py and provider_trace_manager.py provide health, incident and bounded observability primitives.
6. **Persistence and maintenance** — interprocess_lock.py, provider_config_lifecycle.py, catalog_interchange.py, catalog_diff.py and catalog_cli.py provide atomic file operations, validated migration, import/export, snapshots and restore.
7. **Release evidence** — advanced/release_report.py, advanced/pdf_report.py and advanced/verify_wheel.py generate ledger-derived reports, source manifests/SBOMs, a safe source ZIP and reproducible per-platform wheel checks.

## Trust boundaries

- Catalog labels are untrusted metadata until schema and policy validation pass.
- Credentials are referenced through environment/in-memory interfaces and must not be stored in catalogs, reports, traces or package artifacts.
- A generic HTTP success does not prove generation ability, available credits or subscription state.
- Paid execution requires explicit approval and a configured project budget. Ambiguous charge outcomes must not release reservations.
- External probes are injected and must enforce their own network timeouts; a Python callback cannot be forcibly interrupted by the manager.
- JSON managers use atomic replacement and local OS locks, but separate files are not a multi-file transaction and separate devices are not coordinated.
- Output signatures and metadata checks do not replace full media decoding, malware scanning or semantic quality evaluation.

## Operational flow

1. Validate catalog/provider identity and required configuration.
2. Verify permissions, data policy, endpoint allowlist and credential requirements.
3. Confirm runtime connection/health and route compatibility.
4. Reserve applicable budget, credits and quota before a request can start.
5. Execute through the provider adapter with bounded retry/job behavior.
6. Validate output and reconcile actual usage even when a started request fails.
7. Record safe health/incident/metrics events and preserve recovery evidence.

## Production constraints

The 150-point completion ledger and strict release workflow are authoritative. Production readiness must not be inferred from successful unit tests alone. Live provider verification, real billing semantics, representative API/load tests, full-app packaging and remaining repository-wide quality gates require their own evidence.
