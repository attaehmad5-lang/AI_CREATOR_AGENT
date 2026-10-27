# Advanced Provider Store Core

A standard-library-first reference core for provider metadata, model capabilities,
routing simulation, retry/circuit-breaker policy, atomic local JSON storage,
tamper-evident audit logs, explicit paid-usage approval, quotas, budgets,
idempotency, and backup/recovery.

## Safety boundaries

- Unknown prices are not invented. A configured maximum price excludes providers
  whose price is unknown.
- Paid usage requires explicit per-provider approval and is bounded by the budget
  and daily quota.
- Ambiguous provider outcomes are not retried or failed over blindly unless the
  caller explicitly declares the operation idempotent.
- Endpoint validation helpers do not constitute a secure HTTP transport. There
  is no real network client in this core. Do not use the endpoint helpers as a
  substitute for redirect revalidation and connect-time DNS/IP pinning.
- The core's JSON catalog is separate from AI_CREATOR_AGENT's canonical
  ConnectionManager. In this repository, the integrated bridge is the canonical
  read-only selection path; do not create duplicate production catalog/state.
- This package does not store credentials, perform OAuth, or execute paid/live
  provider calls.

## Development checks

From this directory, run:

```sh
python -W error -m unittest discover -s tests -v
python -W error -m compileall -q advps tests
```

The repository CI additionally tests the integrated bridge and ProviderService
contracts on Ubuntu/Windows and Python 3.12/3.13.
