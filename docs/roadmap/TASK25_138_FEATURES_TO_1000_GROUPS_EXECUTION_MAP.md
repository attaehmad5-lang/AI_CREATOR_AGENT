# Task #25 — 138 Canonical Features Mapped to the 1,000 Execution Groups

**Purpose:** make Task #25 executable inside the existing G001–G1000 plan without renumbering or replacing any canonical Task #25 item.
**Authoritative feature names/status:** `projects/provider_store/TASK25_CANONICAL_138_ITEM_LEDGER.md` on `task-25-safe-provider-plugin-lifecycle-20261020`.
**Task #25 execution workstream:** G041–G080 (Provider Connection Manager). Related verification/security/CI groups elsewhere in the 1,000-group plan are dependencies, not replacements for G041–G080.
**Rule:** a group is not VERIFIED merely because this map exists or tests pass. Update its tracker row only with evidence from its own acceptance checks.

## Mandatory execution loop for each group

1. Read the exact canonical feature rows implicated by the group and the real call path in source. Record file/line references.
2. State expected behavior, invalid-input behavior, threat/failure cases, dependencies, and acceptance criteria before changing code.
3. Implement the smallest production-ready slice in the real runtime path. Do not create an isolated mock-only implementation or claim unsupported provider behavior.
4. Add tests: normal path, boundary/invalid input, failure/cleanup, regression, security/privacy, concurrency/idempotency where relevant.
5. Run targeted tests, compile checks, catalog/schema checks, then the relevant full suite and CI. Fix failures and rerun; never treat rerunning alone as a fix.
6. Inspect the diff for unrelated changes and secrets. Never commit `hf_token.txt`, `.env`, credentials, generated outputs, or private data.
7. Update the canonical 138-item ledger and this 1,000-group tracker with exact evidence, commit SHA, commands, platform, counts, CI URL, and remaining blockers.
8. Use VERIFIED only when every acceptance criterion for that group has evidence. Otherwise PARTIAL, OPEN, or BLOCKED. Do not merge PR #77 without explicit user approval.

## Repeatable command set

Run from repository root, in the project’s configured Python environment. If a path or dependency is absent, record the command as blocked and diagnose rather than inventing a passing result.

```bash
python -m py_compile projects/provider_store/connection_manager.py projects/provider_store/provider_plugin_manager.py projects/provider_store/account_balance_probe.py
python -m pytest -q tests/test_connection_manager.py tests/test_provider_plugin_manager.py tests/test_account_balance_probe.py
python -m pytest -q
python tests/check_secret_exclusions.py
```

Also validate every provider JSON catalog using the repository's CI command/script; inspect the workflow file first to use its exact invocation. For CI, record both Ubuntu and Windows outcomes. Live provider checks must use user-authorized credentials and official endpoints only. Do not trigger paid generation, change account state, or rotate credentials without explicit authorization. Android/Pydroid requires a separate device/runtime run and cannot be inferred from Ubuntu/Windows.

## G041–G080: Task #25 group-by-group work instructions

| Group | Execution focus | Canonical Task #25 IDs | Required implementation/review actions | Acceptance evidence |
|---|---|---|---|---|
| G041 | Scope and acceptance baseline | 1–138 | Extract every original feature name/status; define per-item acceptance and explicit external/platform blockers. | All 138 IDs accounted for once; no renumbering; reviewed ledger. |
| G042 | Requirements traceability | 1–138 | Link each item to source, call path, tests, CI evidence and dependencies; mark missing mappings OPEN. | 138 traceability rows with evidence pointers or named gaps. |
| G043 | Architecture and boundary design | 1–8, 17–32, 73–80, 129–138 | Trace catalog → manager → plugin/adapter → routing → generation call path; define trust boundaries and contracts. | Architecture/call-path review plus contract tests; no orphan implementation. |
| G044 | Data model and schema | 1–8, 14, 24, 31, 41–48, 89–96, 129–138 | Validate required/optional fields, types, bounds, aliases, versions, capabilities, deprecation and unknown-field policy. | Valid/invalid catalog fixtures, compatibility tests, corruption tests. |
| G045 | Public/internal interface contract | 8, 17–24, 25–32, 129–130, 136 | Define adapter signatures, modality requirements, version negotiation, stable errors and compatibility behavior. | Positive/negative contract tests for each supported interface version/modality. |
| G046 | Input validation and normalization | 3–4, 9–16, 30–32, 57–72, 89–96 | Reject malformed IDs, endpoints, model names, control characters, unsafe URLs, invalid numeric ranges and invalid policy flags before side effects. | Boundary/fuzz-style tests; invalid input causes no probe, state mutation or secret leakage. |
| G047 | Core implementation slice | 1–8, 9–16, 25–32, 73–80, 129–138 | Implement missing feature slices against the real runtime path in dependency order; no placeholder success returns. | Code-path test demonstrates caller invokes implementation and observes correct result. |
| G048 | Failure and exception semantics | 5–6, 9–16, 33–40, 49–56, 73–80, 97–110 | Normalize errors; fail closed on corrupt catalog/state; cleanup partial loads; preserve charge/quota state if provider may have incurred cost. | Fault-injection tests verify stable codes, cleanup and no unsafe false success. |
| G049 | Security threat analysis | 9–16, 49–72, 97–110, 119–128, 129–138 | Threat-model credential theft, SSRF, redirects, malicious catalog, plugin code, cross-tenant leakage and supply-chain attacks. | Threat register tied to mitigations/tests; sandbox/package-signing gaps remain explicit. |
| G050 | Privacy and secret handling | 6–7, 9–16, 57–72, 81–88, 97–110 | Redact credentials/errors, avoid logging prompts/tokens/IDs unnecessarily, verify secret files are ignored and untracked. | Secret-exclusion test and regression tests for redaction; inspect actual diff/logs. |
| G051 | Configuration and feature flags | 14, 29–32, 41–48, 57–64, 89–96, 111–118 | Validate endpoints/models/regions/policies/budgets/timeouts; safe defaults; reject invalid flags and unsupported config. | Valid config works; invalid config fails closed without network or persisted-state side effects. |
| G052 | Persistence and atomicity | 1–7, 41–48, 57–64, 89–96, 119–124 | Test atomic catalog writes, lock behavior, corrupt-file preservation, backup/restore and interrupted writes. | Crash/corruption/parallel-write tests; document multi-file transaction limitations. |
| G053 | Concurrency and race safety | 3–6, 25–40, 41–48, 57–64, 73–80, 119–128, 129–132 | Stress simultaneous registration/load/unload/discovery/reservation/finalization and status snapshots. | Deterministic concurrency tests with no duplicate registration, stale state or deadlocks. |
| G054 | Idempotency and deduplication | 29, 33–40, 57–64, 73–80, 97–110, 119–128 | Verify repeated request IDs, duplicate reservations/finalization, retry replays and conflict handling. | Same operation is safe to replay; conflicts do not double-charge/release quota. |
| G055 | Timeouts and cancellation | 12–13, 33–40, 41–48, 73–80, 97–110 | Bound connect/read/probe/generation timeouts; propagate cancellation and always clean up safely. | Hung transport/cancel tests finish within bounded time and leave consistent state. |
| G056 | Retry and backoff policy | 33–40, 57–64, 73–80, 97–110 | Validate retryable status classes, Retry-After, capped backoff/jitter, idempotency and paid-charge ambiguity. | 429/5xx/timeout/permanent-error tests; no retry for unsafe/non-idempotent paid calls. |
| G057 | Observability and safe diagnostics | 3, 6–7, 9–16, 41–48, 81–88, 97–110 | Provide bounded counters/status and latency/health records; exclude secrets, raw query strings, prompts and exception text. | Concurrency/privacy tests; telemetry snapshots are defensive and bounded. |
| G058 | Unit tests | 1–138 | Build canonical test matrix for each item; cover success, invalid input, boundaries and failures. | Traceable test matrix; no item marked VERIFIED from unrelated test coverage. |
| G059 | Integration tests | 1–8, 9–32, 41–80, 89–96, 129–138 | Exercise catalog, credentials reference, probe, plugin lifecycle, routing and provider service together using controlled test doubles. | End-to-end local integration tests through actual public call paths. |
| G060 | Fault-injection and recovery tests | 5–6, 10–16, 33–48, 57–64, 89–110, 119–128 | Inject network loss, corrupt JSON, partial writes, factory exceptions, invalid responses, timeouts and interrupted lifecycle transitions. | Every injected failure has bounded, safe, asserted recovery behavior. |
| G061 | Security regression tests | 4, 6–9, 13–16, 49–72, 97–110, 129–138 | Extend regression cases for nested metadata mutation, unsafe IDs/URLs, redaction, catalog corruption, disabled providers and cleanup. | Ubuntu+Windows CI green on the latest commit; security matrix has no unexplained gap. |
| G062 | Performance and resource limits | 3, 12, 33–48, 57–64, 81–88, 119–128 | Measure bounded pagination, catalog size, discovery/load latency, lock contention, memory and response-size limits. | Reproducible benchmark thresholds and stress results; no unbounded reads or loops. |
| G063 | Cross-platform/runtime verification | 1–138 | Verify supported Python versions, Ubuntu, Windows and Android/Pydroid separately; record dependency/platform gaps. | CI matrix evidence plus actual Android/Pydroid run; never infer mobile support from desktop CI. |
| G064 | Accessibility and localization review | 14, 30–32, 57–72, 81–88, 89–96, 136–138 | Verify Unicode-safe names/errors, RTL/Urdu-facing diagnostics and locale-independent machine fields. | Unicode/control-character tests and documented user-facing localization gaps. |
| G065 | Backward compatibility | 1–8, 14, 24, 31, 89–96, 129–138 | Test old catalog schemas, compatibility facades, existing callers and v1.0 adapter declarations. | Existing tests/callers remain valid; explicit migration/deprecation evidence. |
| G066 | Migration and rollback path | 1–8, 41–48, 89–96, 124, 134, 136 | Test catalog version upgrades, backup restore, deprecated provider replacement and rollback. | Restore/migration tests preserve original bytes on failure; rollback documented. |
| G067 | End-to-end workflow integration | 1–32, 33–80, 97–110, 129–138 | Trace request from provider selection through authentication, probe, routing, generation, output validation and usage finalization. | Real local E2E test with test doubles; all state and usage transitions asserted. |
| G068 | External service contract verification | 9–24, 31, 36, 41–48, 57–64, 111–118, 133–134 | Compare each enabled provider's auth, model/capability, balance, rate-limit and deprecation claims with official docs. Run live checks only with authorized credentials. | Per-provider evidence URLs, documented field semantics and live results or explicit BLOCKED. |
| G069 | Quality and semantic validation | 17–24, 49–56, 81–88, 97–110 | Verify outputs by modality: non-empty, correct signature/decoder, bounds, integrity hash, metadata and semantic checks. | Corrupt/empty/wrong-format assets rejected; live modality evidence where authorized. |
| G070 | Documentation and examples | 1–138 | Document setup, supported modalities, auth references, official endpoints, failure codes, costs, limits, safe examples and unsupported cases. | Examples validated against current APIs; no tokens or false claims of free access. |
| G071 | Operational runbook | 33–48, 57–64, 73–80, 81–88, 119–128 | Write procedures for provider outage, invalid credentials, rate limits, corrupt catalog, quota ambiguity and restore. | Runbook steps tested in controlled failure drills. |
| G072 | Audit and evidence capture | 1–138 | Record commands, versions, test counts, commit SHAs, CI URLs, test artifacts, manual checks and remaining gaps. | Evidence is reproducible and linked from ledger/tracker; no unsupported PASS. |
| G073 | Code review and maintainability | 1–138 | Review complexity, types, duplicate logic, dead code, public API compatibility, errors and tests. | Review checklist closed; targeted lint/type checks where repository supports them. |
| G074 | Dependency and supply-chain review | 9–24, 49–72, 129–138 | Inspect dependency pins/advisories/licenses and plugin trust model; document package-signing/isolation limitations. | Dependency/security report and mitigation actions; no arbitrary catalog-driven imports. |
| G075 | Load, stress and soak testing | 3, 12, 33–48, 57–64, 73–80, 119–128 | Run bounded concurrency/load tests for discovery, lifecycle, routing and usage reservation; monitor leakage/drift. | Repeatable results, defined limits, stable cleanup; production soak remains BLOCKED absent deployment. |
| G076 | Deployment and environment integration | 9–16, 41–48, 57–64, 89–96, 119–128 | Validate env config, credentials injection, filesystem permissions, TLS, lock semantics and multi-worker deployment assumptions. | Deployment checklist and environment tests; no claim of cross-container locking without evidence. |
| G077 | Release gate and rollback validation | 97–110, 124, 129–138 | Ensure every canonical item has evidence, CI green, no secrets, no unresolved critical findings, rollback documented. | Release checklist fully satisfied; PR remains unmerged without user approval. |
| G078 | Negative-case completeness sweep | 1–138 | Compare every feature to invalid, boundary, unauthorized, timeout, corrupted-state and duplicate-operation scenarios. | Negative-test matrix and gap list; no missing row hidden by broad suite pass. |
| G079 | Final traceability and sign-off | 1–138 | Reconcile all 138 canonical rows with tests, implementation, live/platform evidence and unresolved blockers. | All rows evidence-backed; no PARTIAL/OPEN/BLOCKED item falsely marked done. |
| G080 | Post-release monitoring and improvement | 41–80, 81–88, 97–110, 119–138 | Define alerts, provider health drift, quota/billing reconciliation, incident feedback and post-release regression review. | Monitoring/runbook/incident drill evidence; release/deployment dependent items remain blocked until available. |

## Current evidence to attach before updating group statuses

- Latest nested-metadata factory-mutation regression commit: `10529f53c6584900a44560f94f656398b0f541a5`.
- Latest CI run for that commit: [#40549998711](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40549998711); Ubuntu and Windows jobs completed successfully.
- This CI pass supports the tested code paths only. It does **not** close all G041–G080 or all 138 canonical features.
- Active Task #25 PR #77 remains draft and must not be merged without explicit user approval.
- The latest canonical ledger/audit remains the source of truth for current gaps, including live provider authentication/balance/generation, all-modality E2E, Android/Pydroid, production load, tenant isolation, and process sandboxing.
