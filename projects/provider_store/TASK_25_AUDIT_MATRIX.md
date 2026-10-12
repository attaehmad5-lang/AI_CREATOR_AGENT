# Task #25 — A–P Verification Matrix

**Status: IN PROGRESS. PR #1 was merged by the user on 2026-10-12; Task #25 is not complete.**

This matrix preserves the canonical feature-ID ranges and group names. It is an evidence tracker, not a replacement for the user's exact 138-item checklist. A group is not complete merely because a supporting module or test exists; every original item still requires individual verification.

## Latest confirmed verification

GitHub Actions run [#38280619566](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/38280619566) passed on Ubuntu and Windows with Python 3.13: **292 tests passed on each platform, 0 failures**. Compilation and provider JSON catalog validation passed. Historical failed runs remain in Actions for audit and were not relabeled as passing.

| Group / IDs | Canonical group | Current evidence | Status | Remaining verification |
|---|---|---|---|---|
| A / 1–8 | Provider Registry & Lifecycle | `provider_catalog.json`, `connection_manager.py`, `test_provider_catalogs.py` | PARTIAL | Audit each of the 8 original items, lifecycle edge cases, and catalog recovery paths. |
| B / 9–16 | Credentials & Connection | `connection_manager.py`, `http_probe.py`, `test_connection_manager.py`, `test_http_probe.py` | PARTIAL | Authorized live provider checks, provider-specific OAuth registration/scopes and token-refresh verification, and each provider's authentication scheme; the generic PKCE helper itself is covered by tests. Generic API-key probes now require `authentication_required=True` for a documented auth-protected endpoint. |
| C / 17–24 | Provider Modalities | Catalog modality contracts and compatibility facades; `test_provider_catalogs.py` | PARTIAL | Real generation adapters and provider-specific capability verification; a callable contract is not a working adapter. |
| D / 25–32 | Routing & Fallback | `routing_manager.py`, `provider_service.py`, `test_routing_manager.py` | PARTIAL | Audit all canonical routing/fallback rules against their required evidence and execution behavior. |
| E / 33–40 | Retry & Failure Handling | `retry_policy.py`, `test_retry_policy.py`, execution failure tests | PARTIAL | Provider-specific transient/network failure injection and end-to-end retry verification. |
| F / 41–48 | Provider Health | `health_manager.py`, `test_health_manager.py` | PARTIAL | Live health probes for each provider and checklist-level verification of freshness, recovery, and thresholds. |
| G / 49–56 | Output Validation | `output_validator.py`, `test_output_validator.py` | PARTIAL | Full media decoding, malware scanning, and semantic/content-quality checks are not provided by signature validation. |
| H / 57–64 | Cost, Credits & Budgets | `cost_policy.py`, `credit_manager.py`, `quota_manager.py`, `pollinations_credit_probe.py`, and corresponding tests | PARTIAL | Pollinations now has a read-only balance adapter for the official `/account/balance` endpoint; live authorized-token/scope verification, other providers' balance integrations, and a single durable transaction across cost, credits, quota, and health remain open. Pollinations uses `gen.pollinations.ai`, `POLLINATIONS_API_KEY`, and no assumed zero-cost access; its OAuth helper requests the `usage` scope, but a verified balance probe is still required before credit-first routing can rely on that balance. |
| I / 65–72 | Security & Privacy | Secret-field redaction, environment-only API credentials, host allowlists, redirect rejection, bounded reads | PARTIAL | Full threat-model review, real-provider security verification, and production credential-storage integration. |
| J / 73–80 | Jobs & Execution | `provider_service.py` execution/finalization facade and regression tests | PARTIAL | End-to-end runs with real generation adapters and recovery of ambiguous external requests. |
| K / 81–88 | Performance & Observability | Interprocess locking, bounded health history, retry metadata, test coverage | PARTIAL | Production telemetry/metrics, load tests, and multi-worker storage validation. |
| L / 89–96 | Configuration & Recovery | `configure_provider()`, schema validation, master-catalog restore | PARTIAL | Per-item configuration/recovery audit; secrets, sessions, and ledgers are intentionally not restored by catalog recovery. |
| M / 97–110 | Integration & Reliability | `provider_service.py`, compatibility tests, cross-platform CI | PARTIAL | End-to-end provider integration, failure injection, and cross-manager recovery/transaction tests. |
| N / 111–118 | Advanced Routing | Cost/credit/health-aware routing and round-robin foundations in `routing_manager.py` | PARTIAL | Verify every advanced strategy against canonical requirements and realistic provider outcomes. |
| O / 119–128 | Usage & Management | Reservation, quota, cost reconciliation, and credit accounting managers | PARTIAL | Complete usage/reporting and management requirements from the canonical checklist. |
| P / 129–138 | Extensibility & Administration | Central registry and legacy/unified compatibility facades | PARTIAL | Verify provider/plugin lifecycle requirements and any required administration surfaces against the original items. |

## Non-negotiable completion gates

1. Preserve the exact canonical 138-item A–P list; do not infer individual item names from group headings.
2. Mark each original item individually only after code evidence and relevant tests exist.
3. Fix and rerun every failing test; no failure is ignored or marked passed without a rerun.
4. Complete live-provider checks only with user-authorized credentials and safe, provider-specific probes. A generic HTTP 2xx is not proof of authenticated access unless the configured endpoint is documented to enforce the credential.
5. PR #1 has already been merged by the user. Do not mark Task #25 complete; use a follow-up branch/PR for remaining work and do not merge follow-up changes without explicit user approval.


## Canonical 138-item checklist preservation

- The authoritative scope remains the user's original 138 individual feature descriptions across sections A–P, originally marked 6 🟢, 29 🟡, and 103 🔴.
- This matrix currently preserves section names and item-number ranges, but does **not** contain all 138 original descriptions verbatim. Do not invent descriptions from the group headings or silently renumber/merge items.
- Feature-by-feature completion requires the original wording to be copied verbatim into a dedicated checklist file, then each item tracked separately with implementation path, test evidence, verification result, and backup/commit reference.
- Workflow for every item: **Scope → Build → Test → Fix failures → Rerun → Verify → Save evidence**. A test failure remains unresolved until a confirming rerun passes.
