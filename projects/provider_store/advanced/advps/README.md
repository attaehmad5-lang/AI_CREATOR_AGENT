# Advanced Provider Store Core

A standard-library-first core for provider metadata, model capabilities,
routing simulation/execution through injected callbacks, retry/circuit-breaker
policy, atomic local JSON storage, tamper-evident audit logs, explicit
paid-usage approval, quotas, budgets, idempotency, and backup/recovery.

## Safety boundaries

- Unknown prices are not invented. A configured maximum price excludes providers
  whose price is unknown.
- Paid usage requires explicit per-provider approval and is bounded by the budget
  and daily quota.
- Ambiguous provider outcomes are not retried or failed over blindly unless the
  caller explicitly declares the operation idempotent.
- `SecureHTTPTransport` provides bounded HTTPS requests, redirect revalidation,
  public-address validation, connect-time DNS/IP pinning, TLS hostname checking,
  and request/response size limits. Unit tests use fake connections; no live
  provider calls were made. Production adoption still requires security review,
  integration with ProviderService, and deployment-specific tests.
- The transport is not yet wired into ProviderService execution or credential
  lifecycle. Do not treat the standalone CLI catalog as a second production
  source of truth.
- The core's JSON catalog is separate from AI_CREATOR_AGENT's canonical
  ConnectionManager. In this repository, the integrated bridge is the canonical
  read-only selection path; do not create duplicate production catalog/state.
- This package does not store credentials or implement OAuth token rotation.

## Development checks

The repository test suite for the committed core is `tests/test_advps_core.py`.
Run from the repository root:

```sh
python -W error -m unittest discover -s tests -p "test_advps_core.py" -v
python -W error -m compileall -q projects/provider_store/advanced/advps
python projects/provider_store/advanced/tools_coverage.py 80
```

The repository CI additionally tests the integrated bridge and ProviderService
contracts on Ubuntu/Windows and Python 3.12/3.13.
