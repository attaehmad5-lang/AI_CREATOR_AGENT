# Advanced Provider Store Core

A standard-library-first core for provider metadata, model capabilities,
routing simulation/execution through injected callbacks, bounded HTTPS
transport, environment-backed credential references, provider health/probe
leases, retry/circuit-breaker policy, atomic local JSON storage, tamper-evident
audit logs, explicit paid-usage approval, quotas, budgets, idempotency, job
lifecycle/leases/recovery, and backup/recovery.

## Safety boundaries

- Unknown prices are not invented. A configured maximum price excludes providers
  whose price is unknown.
- Paid usage requires explicit per-provider approval and is bounded by the budget
  and daily quota.
- Ambiguous provider outcomes are not retried or failed over blindly unless the
  caller explicitly declares the operation idempotent.
- `SecureHTTPTransport` provides bounded HTTPS requests, redirect revalidation,
  public-address validation, connect-time DNS/IP pinning, TLS hostname checking,
  and request/response size limits. Cross-origin redirects strip credential-like
  headers. Unit tests use fake connections/resolvers; no live provider calls were made.
- The transport is not yet wired into ProviderService execution and needs
  controlled TLS/DNS-rebinding/adversarial network tests before production use.
- `CredentialManager` resolves environment references and supports an injected
  refresh callback; raw credentials are held only in process memory and are never
  persisted by this module. OAuth/network policy remains the host's responsibility.
- `HealthRegistry` records sanitized outcomes, schedules due probes, manages
  probe leases, failure thresholds, backoff and latency EWMA. The host must supply
  actual probe callbacks and worker execution.
- `JobManager` persists job metadata, hashed idempotency keys, leases, attempts,
  cancellation state and stale-lease recovery. It does not store payloads or
  credentials, and it does not itself interrupt an executing worker process.
- The core's JSON catalog is separate from AI_CREATOR_AGENT's canonical
  ConnectionManager. In this repository, the integrated bridge is the canonical
  read-only selection path; do not create duplicate production catalog/state.

## Development checks

Run from the repository root:

```sh
python -W error -m unittest discover -s tests -p "test_advps_core.py" -v
python -W error -m compileall -q projects/provider_store/advanced/advps
python projects/provider_store/advanced/tools_coverage.py 80
```

The repository CI additionally tests the integrated bridge and ProviderService
contracts on Ubuntu/Windows and Python 3.12/3.13.
