# Task #25 — Remaining Work & Cloud AI Handoff Plan

**Purpose:** Single actionable handoff for finishing all remaining entries in the canonical 138-item ledger. The canonical ledger remains the source of truth; do not renumber items or silently change their names/statuses.

**Repository:** `attaehmad5-lang/AI_CREATOR_AGENT`  
**Working branch:** `task25-portable-health-gate-fix-20261025` (PR #79; draft; do not merge without the owner's explicit approval).  
**Canonical ledger:** `projects/provider_store/TASK25_CANONICAL_138_ITEM_LEDGER.md`

## Snapshot at creation time

- Total canonical items: **138**
- VERIFIED: **29**
- PARTIAL: **103**
- OPEN: **6**
- Fully verified: **21.0%**
- Work not yet fully verified (PARTIAL + OPEN): **109 items (79.0%)**
- This is an evidence-based ledger count, not a claim that the whole product is production complete. Recalculate after every batch from the current ledger.

## Who does what

### Cloud AI / coding worker — implementation owner
For each item below:
1. Read the existing implementation and its callers before changing code; reuse existing patterns rather than adding duplicate managers.
2. Implement the smallest complete, production-safe change that closes the listed gap. Do not fake providers, fabricate live evidence, hardcode secrets, or weaken fail-closed security controls.
3. Add or update focused unit tests, negative/security tests, persistence/restart tests where relevant, and integration tests through the real service entry point.
4. Run the focused tests, then the full test suite and compile/JSON/schema checks. Fix regressions before moving on.
5. Update the item's **Current audit** to VERIFIED only when the full original requirement is covered by code and tests. Otherwise leave PARTIAL/OPEN and document exactly what remains.
6. Commit small logical batches to this branch and report commit SHA, changed files, test commands/results, and remaining blockers. Do not merge PR #79.

### ChatGPT / review & verification worker
- Independently inspect each code diff and tests; challenge unsafe assumptions and tests that only test mocks.
- Check authorization, tenant isolation, secret handling, privacy, billing/no-charge behavior, timeout/cancellation, path portability, concurrency, persistence, atomicity, and error sanitization where relevant.
- Re-fetch the canonical ledger and calculate counts from its actual 138 numbered rows; verify that status changes match evidence.
- Inspect GitHub Actions for the exact latest commit; require Ubuntu and Windows jobs to pass.
- Keep external credentials, live provider access, deployment, secret-vault setup, real-model inference and production rollout explicitly marked as blockers if not actually available. Never claim these were live-tested if they were not.
- Do not merge or deploy without explicit owner approval.

### Shared working rule (important)
Only one worker should edit a given file at a time. Before writing, fetch the latest file/branch SHA; after another worker commits, re-fetch and rebase/reconcile rather than overwriting their work. Keep secrets out of commits and logs. If Cloud AI is operating in a separate checkout/branch, it must push its branch and provide the commit SHA/PR; do not assume two agents can directly share unsynchronized working memory.

## Definition of done for each item
- [ ] Requirement is traced to the original ledger item number and canonical wording.
- [ ] Code implements the intended behavior end-to-end through the real entry point.
- [ ] Positive, negative, malformed-input, and authorization tests exist as appropriate.
- [ ] Failure paths are fail-closed and do not leak prompts, tokens, raw provider bodies, or tenant data.
- [ ] Resource/billing side effects are correct on success, failure, cancellation, timeout, and uncertain provider outcome.
- [ ] Persistence/restart, race/concurrency, and path-portability tests exist where applicable.
- [ ] Focused tests and full CI pass on supported platforms.
- [ ] Evidence link / test names and known limits are recorded in the canonical ledger.
- [ ] Status remains PARTIAL/OPEN if real credentials, provider-specific live behavior, decoders, deployment, or other required evidence is missing.

## Execution order
Work in short batches; finish and test a batch before starting the next. Suggested order:
1. **Security and accounting foundations:** authentication/OAuth/refresh, authorization, secret storage/logging, tenant/user/project policy, paid approval, quotas/budgets, and safe billing error handling.
2. **Provider runtime foundations:** registry as source of truth, registration/onboarding, metadata refresh, model/version/capability routing, retries, deadlines/cancellation, health probe/recovery orchestration.
3. **Output integrity:** image/audio/video validation, atomic output lifecycle, preservation, lineage, size/dimension/duration/codec limits.
4. **Actual modalities:** text/cloud model, image, video, voice/audio, music, editing/upscaling, transcription/translation/dubbing; implement adapters only for documented providers with available credentials and verify output end-to-end.
5. **Operations and production evidence:** external notifications, managed secrets, SLOs, backups/restore, security review, deployment/runbook, rollback, real credentials and live smoke tests.
6. **Final verification:** complete all cross-platform tests, recalculate 138-item ledger, audit each remaining PARTIAL/OPEN item, then owner decides whether to merge/release.

## Remaining item-by-item backlog


### A — Basic Provider Management

#### #1 — Provider Registry [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_catalog.json`, `connection_manager.py`, catalog/schema tests. The separate generation runtime now resolves its default registry relative to its module and rejects malformed root/image sections, but the provider-store catalog is not yet the single source of truth for runtime adapter activation; portability/schema regressions pass on Ubuntu and Windows in run [#40391867877](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40391867877).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #2 — Provider Registration [PARTIAL]

- **Current evidence / gap from canonical ledger:** Registration now validates the complete candidate catalog before persistence, rejecting unsupported auth types and malformed capability metadata without writing the provider; regression tests cover invalid registration and persistence safety. Onboarding and runtime adapter registration remain incomplete. Evidence: `tests/test_connection_manager.py::test_register_provider_rejects_catalog_schema_violations_without_persisting`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #7 — Provider Metadata [PARTIAL]

- **Current evidence / gap from canonical ledger:** Metadata/schema validation exists; live capability/version/price metadata refresh is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### B — Authentication and Connection

#### #9 — API Key/Token Authentication [PARTIAL]

- **Current evidence / gap from canonical ledger:** Environment-only credential references and bounded HTTP probes exist; provider-specific auth schemes require live verification.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #10 — OAuth/Login Support [PARTIAL]

- **Current evidence / gap from canonical ledger:** Generic OAuth 2.0 PKCE helper is tested; provider-specific registration, consent and scopes are not live-verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #11 — Credential Expiry and Refresh [PARTIAL]

- **Current evidence / gap from canonical ledger:** Token expiry/refresh lifecycle primitives exist; OAuth provider lookup errors now fail closed without leaking storage details. Provider-specific refresh semantics and revoked-token recovery remain open. Evidence: `tests/test_provider_service.py::test_oauth_verification_fails_closed_when_provider_lookup_raises`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #12 — Connection and Network Timeouts [PARTIAL]

- **Current evidence / gap from canonical ledger:** Bounded timeout validation and tests exist; full generation cancellation/deadline propagation remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #13 — Live Connectivity Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** Generic authenticated HTTPS probe and local Hugging Face check are documented; no live credential test was run in CI.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #15 — TLS/HTTPS/Certificate Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** HTTPS validation and standard-library TLS verification are used; deployment-specific certificate/pinning review remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #16 — Authentication, Permission and Access Error Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** Safe 401/403 handling exists; provider-specific permission/scope errors need live verification.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### C — Multimodal Provider Support

#### #17 — Image Generation Providers [PARTIAL]

- **Current evidence / gap from canonical ledger:** Hugging Face and Pollinations are cataloged; this task's manager tests do not prove live generation outputs.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #18 — Video Generation Providers [OPEN]

- **Current evidence / gap from canonical ledger:** No verified working video adapter and end-to-end generation test in this manager scope.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #19 — Voice/Speech/Audio Providers [OPEN]

- **Current evidence / gap from canonical ledger:** No verified provider-specific voice/audio generation adapter and live test.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #20 — Text/Script/Language Model Providers [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added an Ollama-compatible local text adapter with numeric-loopback-IP-only endpoints, bounded timeouts/response size, explicit `done: true` completion validation, and no prompt leakage in error records. `ProviderService` validates and allowlists adapter results. Regression tests pass on Ubuntu/Windows in run [#40364204249](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40364204249). Provider-specific cloud text/script adapters and real local-model end-to-end verification remain incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #21 — Music Generation Providers [OPEN]

- **Current evidence / gap from canonical ledger:** No verified music generation adapter and output validation test.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #22 — Image Editing/Upscaling/Restoration [OPEN]

- **Current evidence / gap from canonical ledger:** No verified end-to-end editing/upscaling/restoration adapters.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #23 — Transcription, Translation and Dubbing [OPEN]

- **Current evidence / gap from canonical ledger:** No verified end-to-end transcription/translation/dubbing adapters.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #24 — Future Modality Support and Contract Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Capability identifiers and callable modality contracts are tested; working adapters for future modalities remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### D — Routing and Selection

#### #30 — Capability-Based Routing [PARTIAL]

- **Current evidence / gap from canonical ledger:** Capability filtering exists and is tested at discovery; the complete canonical routing/metadata truth remains incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #31 — Model/Version-Aware Routing [PARTIAL]

- **Current evidence / gap from canonical ledger:** Router and catalog schema now support declared model IDs/versions and fail closed when metadata is absent; live model catalogs are not populated for enabled providers.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #32 — User/Project Policy [PARTIAL]

- **Current evidence / gap from canonical ledger:** Project IDs and policy gates exist; complete user/project policy enforcement is not verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### E — Retry, Failure and Recovery

#### #34 — Error-Specific Retry Policy [PARTIAL]

- **Current evidence / gap from canonical ledger:** Retryability/status mapping exists; provider-specific transient failure coverage is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #37 — Quota Exhaustion Detection [PARTIAL]

- **Current evidence / gap from canonical ledger:** Quota manager and routing rejection paths are tested; provider-side quota semantics remain incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #39 — Cooldown and Provider Recheck [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderRecoveryCoordinator` schedules explicit rechecks only for due UNHEALTHY/DEGRADED providers, enforces cooldown, rejects future/malformed timestamps, fails closed on corrupt snapshots, and avoids generation/routing side effects. `ProviderService.run_due_provider_rechecks()` exposes the explicit operation and now checks `provider.verify` authorization before coordinator invocation whenever an authorization manager is configured; denied requests are sanitized and do not trigger rechecks. Regression coverage: `tests/test_recovery_coordinator.py` and `tests/test_provider_service.py`, including denied/allowed authorization. Snapshot parsing rejects duplicate keys/non-finite numbers, bounds actual reads to 2 MiB, validates timezone-aware clocks, and rejects huge cooldown values without overflow. Full background scheduling and production deployment integration remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #40 — Timeout, Cancellation and Stalled-Request Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** Timeout handling is tested; cancellation, deadlines and stalled-job recovery are incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### F — Health and Reliability

#### #43 — Health Check Before Generation [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderService.execute_routed_request(..., require_fresh_health_check=True)` now checks recorded eligibility before adapter invocation and fails closed on missing/stale/recovering/unhealthy/slow/malformed evidence or an unavailable manager; tests cover blocked unknown health and permitted fresh evidence. A separate opt-in gate now also protects `ProviderService.generate_local_text()` and validates an explicit provider ID; missing, stale, malformed, or exception-producing health evidence blocks adapter invocation. Latest local-text health-gate regression evidence: run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191). Both paths remain opt-in, use stored evidence rather than running a live probe, and are not yet enforced across every generation entry point. Evidence: `tests/test_provider_service.py::test_routed_generation_can_require_fresh_health_evidence`, run [#40379755049](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40379755049).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #45 — Active Health Probes [PARTIAL]

- **Current evidence / gap from canonical ledger:** Generic HTTPS probe exists; provider-specific authenticated probes and live results remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #47 — Provider Recovery Detection [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderHealthManager` supports a configurable consecutive-success recovery threshold; providers recovering from `UNHEALTHY` remain ineligible as `RECOVERING` until enough successful probes are recorded. Default threshold preserves existing one-success behavior; full automated probe scheduling remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #48 — Stale Health and Consistency Checks [PARTIAL]

- **Current evidence / gap from canonical ledger:** Stale/future health evidence is excluded, malformed persisted-state checks fail closed, and `ProviderHealthManager.audit_consistency()` now performs a read-only audit for conflicting status/counters, impossible recovery state, future timestamps, invalid latency, and non-chronological history. Five regression tests cover consistent empty state, counter conflicts, threshold mismatch, future timestamps without repair, and history ordering; run [#40446263623](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40446263623) passes 627 tests on Ubuntu and Windows. Full automated health reconciliation, scheduled re-probing, and safe repair workflows remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### G — Output and Asset Safety

#### #49 — Image Output Validation [PARTIAL]

- **Current evidence / gap from canonical ledger:** Signature/basic file validation is tested; real provider images and decoder-level validation remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #51 — Audio/Video Format, Codec and Integrity Validation [PARTIAL]

- **Current evidence / gap from canonical ledger:** `media_integrity_validator.py` validates WAV frame/data integrity and bounded MP4/WebM/Ogg structure; MP4/WebM/Ogg reads now cap actual bytes read and reject files that grow or change size after the initial stat. `ProviderService` can reject malformed audio/video containers after basic output validation. Full codec decoding, codec allowlists, and semantic media validation remain open. Evidence: `tests/test_media_integrity_validator.py`, run [#40486096062](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40486096062).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #52 — Corrupt/Incomplete Output Rejection [PARTIAL]

- **Current evidence / gap from canonical ledger:** Basic corrupt/truncated artifact rejection is tested; decoder-level detection remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #53 — Temporary File Cleanup and Atomic Replacement [PARTIAL]

- **Current evidence / gap from canonical ledger:** Atomic replacement helpers exist; output-pipeline cleanup integration is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #54 — Previous Output Preservation on Failure [PARTIAL]

- **Current evidence / gap from canonical ledger:** Some preservation/atomicity tests exist; end-to-end generation replacement behavior is not verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #55 — File Size, Dimensions, Duration and Codec Constraints [PARTIAL]

- **Current evidence / gap from canonical ledger:** Size limits exist; dimensions/duration/codec constraints need modality-specific decoders.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #56 — Provider Attribution and Output Metadata [PARTIAL]

- **Current evidence / gap from canonical ledger:** Request/provider IDs exist in service records; complete asset lineage metadata is not verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### H — Cost, Free Credits and Quotas

#### #57 — Free-Credit-First Routing [PARTIAL]

- **Current evidence / gap from canonical ledger:** Routing and credit reservations exist; provider-specific verified free-credit balances are still required.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #58 — Provider Free-Tier/Quota Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** Generic JSON account-balance probe exists; provider-specific documented fields and authorized live checks are missing.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #59 — Explicit Approval Before Paid Usage [PARTIAL]

- **Current evidence / gap from canonical ledger:** Paid routing now rejects a non-empty caller-supplied `paid_approved_providers` set unless `paid_usage_approved=True` is explicitly supplied and the authenticated principal passes `billing.approve`; a forged provider set alone cannot authorize spending. Regression tests cover forged-set rejection and missing billing permission. Durable approval requests, approver/requester separation, expiry, and revocation remain incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #60 — Per-Request/Project Budgets [PARTIAL]

- **Current evidence / gap from canonical ledger:** Project budget and quota managers are tested; end-to-end tenant/user budgeting is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #61 — Estimated vs Actual Usage Reconciliation [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cost reservation/reconciliation is implemented and tested; provider-specific actual usage adapters are missing.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #62 — Credit Exhaustion and Billing Error Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_error_policy.py` normalizes explicit billing/credit/auth/permission/rate-limit/transient signals and recognizes additional credit/budget aliases (`insufficient_balance`, `no_credits_remaining`, `credit_limit_exceeded`, `monthly_budget_exceeded`, `billing_limit_exceeded`) plus billing-state aliases; focused tests assert these never retry or expose raw provider codes. `ProviderService.execute_routed_request` blocks retry for non-retryable billing/credit outcomes, including HTTP 429 with `insufficient_quota`. Provider-specific official mappings and authorized live account tests remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #63 — Cost-Aware Provider/Model Selection [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cost/credit-aware routing foundations exist; reliable live prices and model-level cost comparisons remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #64 — Budget Alerts and Hard-Stop Policy [PARTIAL]

- **Current evidence / gap from canonical ledger:** `BudgetAlertManager` persists idempotent 50/80/100% threshold events, keeps failed deliveries pending with bounded attempt metadata, and supports injected notification dispatch with retry-safe outcomes. External channel setup, scheduling, and end-to-end delivery monitoring remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### I — Security, Privacy and Governance

#### #65 — Secrets Manager Integration [PARTIAL]

- **Current evidence / gap from canonical ledger:** Environment variable references are supported; managed secret vault/rotation integration is not complete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #66 — Secret/Token Leak Prevention in Logs [PARTIAL]

- **Current evidence / gap from canonical ledger:** Credential redaction and no-response-body probes are tested; full application log scan is pending.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #67 — Least-Privilege Access Control [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_authorization.py` persists explicit viewer/creator/operator/billing-manager/admin role assignments and denies unknown principals/operations; `ProviderService` checks generation and verification permissions, authorizes provider enable/disable/configuration management, and separately requires `billing.approve` for paid activation. Tenant-bound resource checks, broader management endpoints, and end-to-end identity-provider integration remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #68 — Provider Permissions and Allowed Operations [PARTIAL]

- **Current evidence / gap from canonical ledger:** Explicit role-to-operation permissions are persisted; `ProviderService` checks `provider.generate` before execution, `provider.verify` before probes, and `provider.enable`/`provider.disable`/`provider.configure` before management operations. Provider-specific scopes and authorization for other management endpoints remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #69 — Sensitive-Data Detection and Privacy Enforcement [PARTIAL]

- **Current evidence / gap from canonical ledger:** `sensitive_data_policy.py` detects credential-like tokens, email addresses and phone numbers with bounded traversal, returns only categories/paths (never matched values), and blocks configured sensitive categories before `ProviderService` routes or invokes an adapter. Broader data-at-rest/retention enforcement and end-to-end privacy controls remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #70 — Data Retention and Provider Data-Handling Rules [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_data_policy.py` requires explicit HTTPS-sourced provider terms, allowed data categories, maximum retention, and training-use permission/consent; routed requests are blocked when a configured policy is missing or rejects the request. Local text generation now also passes an explicit provider ID, categories, integer retention period, provider-training flag, and user consent through the policy gate and fails closed on denial/exception; latest local-text integration regressions pass in run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191). Provider-specific verified policy catalog coverage, mandatory policy-manager configuration, and downstream deletion/retention enforcement remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #71 — Provider Endpoint Allowlisting and Network Security [PARTIAL]

- **Current evidence / gap from canonical ledger:** HTTPS host allowlists, redirect rejection and bounded reads are tested; deployment threat-model review remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #72 — Routing, Credential and Approval Audit Logs [PARTIAL]

- **Current evidence / gap from canonical ledger:** Provider activation decisions now append structured, secret-free JSONL audit events with a SHA-256 hash chain under a cross-process lock; allowed/denied decisions, tampering, and audit-write failure blocking are tested. Full audit coverage for routing/credential/approval events, retention/rotation, external tamper anchoring, and distributed trace linkage remain incomplete. Evidence: `ConnectionManager._append_audit_event`, `tests/test_connection_manager.py::ProviderPolicySimulationTests`, run [#40197656173](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40197656173).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### J — Production Orchestration

#### #73 — Unique Request ID and Trace ID [PARTIAL]

- **Current evidence / gap from canonical ledger:** Request IDs are now restricted to 1–128 safe ASCII characters before routing/reservation; `execute_routed_request` also validates optional trace IDs and propagates them to adapters and returned execution outcomes. Full distributed trace collection remains incomplete; untrusted trace IDs hidden in adapter options are discarded. Cross-service propagation through remote jobs, webhooks, and external telemetry remains incomplete. Evidence: `tests/test_provider_service.py::test_execution_records_latency_queue_wait_and_failure_metrics`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #74 — Idempotency and Duplicate Prevention [PARTIAL]

- **Current evidence / gap from canonical ledger:** Manager-level idempotency is tested; full external-provider request idempotency is not verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #75 — Concurrency and Provider-Specific Worker Limits [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cross-process reservations and locking are tested; worker pools and provider-specific concurrency limits are incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #76 — Queue, Backpressure and Overload Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_job_manager.py` implements a persistent bounded queue, priority aging, concurrency limits, backpressure retry hints, idempotent submission, lease recovery, and cancellation-aware lease renewal. `ProviderService.renew_job_lease()` applies tenant membership and `provider.generate` authorization before renewal. Invalid/missing/naive leases fail safe; expired workers cannot commit stale results or overwrite a retry. Tests cover stale completion, malformed leases, renewal ownership/expiry/cancellation, and cross-tenant denial. Production distributed queue scaling and sustained load tests remain open. Evidence: `tests/test_provider_job_manager.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #77 — Async/Long-Running Job and Polling Support [PARTIAL]

- **Current evidence / gap from canonical ledger:** Persistent `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` states, polling, worker leases, bounded retries, cancellation, result references, and live lease renewal are tested. Expired workers are rejected even before a retry is claimed; corrupt/missing leases are reaped safely, and renewal is gated by ownership, tenant authorization, and cancellation state. Provider-specific remote job polling/webhook orchestration remains open. Evidence: `tests/test_provider_job_manager.py`, `tests/test_provider_service.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #78 — Webhook/Callback Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_webhook_verifier.py` validates HMAC-SHA256 signatures with constant-time comparison, bounded timestamps/payloads, and persistent event replay rejection; `ProviderService.verify_webhook` delegates without echoing secrets. Provider-specific header/signature conventions and production callback ingress integration remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #79 — Cancellation, Deadlines and Graceful Shutdown [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cancellation requests are honored during lease recovery and prevent lease renewal; authorized workers can renew a still-live lease. End-to-end cancellation/deadline propagation and graceful worker-pool shutdown remain incomplete. Evidence: `tests/test_provider_job_manager.py`, `tests/test_provider_service.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #80 — Generation State Manager/Orchestration Integration Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cross-component restart recovery remains tested, and the underlying queue now rejects expired worker completion and safely reaps malformed leases. Broader Generation State Manager/Executor/Orchestrator/Retry Manager integration and crash-recovery matrices remain incomplete; Task #29 is not declared complete. Evidence: `tests/test_provider_service.py::test_job_state_recovers_expired_worker_lease_across_service_restart`, `tests/test_provider_job_manager.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### K — Quality, Performance and Observability

#### #82 — Provider Success/Error-Rate Metrics [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_metrics_manager.py` records success/failure/timeout counters and success rate from provider executions; bounded history avoids payload storage. Metrics collection now rejects failing clocks, naive/unsupported timestamps, and durations exceeding seven days so invalid values cannot poison aggregates. Export to production telemetry remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #83 — Throughput, Queue-Wait and Timeout Metrics [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderMetricsManager` aggregates bounded latency percentiles, queue-wait averages, timeout counts, and sample throughput; `ProviderService` records execution latency and queue wait. External telemetry export and load-tested throughput metrics remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #84 — Output Quality Validation/Score Integration [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_quality_policy.py` adds an injected output scorer and `ProviderService` applies its result after file validation; below-threshold or invalid scores prevent an output from being reported successful. A production semantic scorer and modality-specific QA remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #86 — Provider/Model Benchmarking [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_benchmark_manager.py` aggregates bounded provider/model samples by success rate, p50/p95 latency, cost per attempt/success, and optional quality score; `ProviderService` can record execution samples when a model ID is available. Controlled benchmark suites, sample-size confidence, and live provider comparisons remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #87 — Structured Logs, Metrics and Distributed Traces [PARTIAL]

- **Current evidence / gap from canonical ledger:** Some structured status metadata exists; end-to-end metrics/tracing is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #88 — Alerts, Dashboards and Incident Diagnostics [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_incident_manager.py` persists deduplicated provider incidents with severity escalation, occurrence counts, acknowledgement/resolution, and safe status summaries; `ProviderService` opens critical incidents for explicit billing/credit exhaustion and records other execution failures. Dashboard and external alert delivery remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### L — Configuration, Compatibility and Change Control

#### #90 — Safe Defaults and Invalid-Configuration Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** Fail-closed validation exists; complete configuration recovery/default audit remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #91 — Configuration Versioning and Migration [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_config_lifecycle.py` requires explicit version-to-version migration functions, validates each intermediate configuration, and writes migrated JSON via atomic replacement with a backup. Repository-wide migration coverage and cross-process crash-injection tests remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #92 — Safe Hot Reload [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderConfigLifecycleManager` validates candidate configuration before swapping active state, preserves the last-known-good configuration, and supports rollback after reload failures. Integration into every runtime catalog consumer remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #93 — Feature Flags and Controlled Rollout [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_feature_flags.py` persists kill switches, project allow/deny lists, and deterministic request-scoped percentage rollout; `ProviderService` blocks generation when `provider_generation` is disabled or outside rollout. Admin authorization/audit integration and cross-service rollout coordination remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #94 — Provider API/Model Version Compatibility [PARTIAL]

- **Current evidence / gap from canonical ledger:** Model/endpoint validation exists; live API/model version compatibility matrix is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #95 — Rollback to Known-Good Configuration [PARTIAL]

- **Current evidence / gap from canonical ledger:** Catalog restore exists; runtime policy rollback is not fully verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #96 — Configuration Backup/Restore Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** Master catalog backup/restore is tested; full config/state backup verification is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### M — Testing, Audit and Release Readiness

#### #97 — Routing/Helper Unit Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** 559-test cross-platform suite covers helper/routing paths; exact item-level coverage matrix remains incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #101 — Failure/Success Simulation Coverage [PARTIAL]

- **Current evidence / gap from canonical ledger:** Mock success/failure and persistence-failure tests exist; provider-specific network injection remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #102 — Invalid Output, Cleanup and File-Preservation Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Output validation tests exist; cleanup/preservation across all generation writers is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #103 — Concurrency, Idempotency and Cancellation Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cross-process manager and idempotency tests exist; cancellation and full job orchestration are incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #104 — Security, Secret-Leak and Privacy Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Credential redaction/endpoint tests exist; full privacy and secret scanning remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #105 — Cost Policy and Paid-Approval Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cost/budget/approval tests pass; provider billing reconciliation remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #106 — Every Supported Modality End-to-End Tests [OPEN]

- **Current evidence / gap from canonical ledger:** Real end-to-end tests for all modalities do not exist yet.
- **Implementation steps:**
  1. Inspect existing interfaces, provider catalog and adjacent tests; document the expected contract before implementation.
  2. Implement the missing feature through the real service/adapter entry point; add configuration and safe failure handling without dummy success responses.
  3. Add positive, negative, malformed-input, authorization/privacy and integration tests as relevant; include deterministic fake transport for unit tests and live smoke test only when approved credentials/environment exist.
  4. Run focused tests and full cross-platform CI; record exact commands, test names and live-test limitations.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #107 — Failure Injection and Recovery Tests [PARTIAL]

- **Current evidence / gap from canonical ledger:** Persistence/network error cases are tested; comprehensive cross-manager recovery injection remains open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #108 — Full Regression Suite and Reproducible Test Reports [PARTIAL]

- **Current evidence / gap from canonical ledger:** 559 tests pass on Windows and Ubuntu, with JSON report artifacts uploaded for both operating systems; complete 138-item coverage mapping and production evidence remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #109 — Git Commit, Secret-Exclusion Check and Remote Backup Verification [PARTIAL]

- **Current evidence / gap from canonical ledger:** Changes are committed and reviewed in PR #7; dedicated secret scanning and backup verification are not complete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #110 — Production Release Checklist and Final PASS [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `ProviderReleaseReadiness` to fail closed unless all 138 features are VERIFIED, PARTIAL/OPEN counts are zero, and explicit evidence is supplied for cross-platform tests, every modality E2E, live credentials, secret scan, remote backup, restore/rollback, security review, and approved operations runbook. Current ledger still has incomplete features, and live multimodal E2E/production evidence is not complete; no final PASS is claimed. Evidence: `tests/test_provider_release_readiness.py`, run [#40202197447](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40202197447).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### N — High-Advanced Routing Intelligence

#### #111 — Multi-Factor Routing: Quality, Latency, Cost and Capability [PARTIAL]

- **Current evidence / gap from canonical ledger:** Cost/health/capability foundations exist; unified quality-aware scoring is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #112 — Project-Specific Provider Selection [PARTIAL]

- **Current evidence / gap from canonical ledger:** Project identifiers and policies exist; full per-project selection policy is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #113 — Automatic Exclusion of Providers That Cannot Meet Constraints [PARTIAL]

- **Current evidence / gap from canonical ledger:** Capability/health/credit filtering exists; all constraint classes are not covered.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #114 — Confidence-Aware Routing and Uncertainty Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderRoutingManager.route(confidence_aware=True)` requires model-specific benchmark evidence, configurable minimum sample count/success rate/optional p95 latency, and either explicitly allowed deterministic fallback or fail-closed `CONFIDENCE_INSUFFICIENT`. Confidence-aware route decisions expose bounded benchmark evidence. Calibration and production confidence intervals remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #115 — Controlled A/B Benchmarking Without Unintended Paid Usage [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `ProviderABBenchmarkManager` to build side-effect-free A/B plans, default to zero-cost candidates, reject duplicate/malformed candidates, calculate total estimated cost across repetitions, enforce the total budget, and require paid-use opt-in plus a matching approval record. `ProviderService.plan_ab_benchmark()` checks the authenticated principal for `billing.approve` and discards caller-supplied approval objects; the planner never calls providers. An authorized execution runner, real provider cost reconciliation, and controlled live A/B comparison remain incomplete. Evidence: `tests/test_provider_ab_benchmark_manager.py`, `tests/test_provider_service.py::test_ab_benchmark_paid_usage_requires_authenticated_billing_approval`, run [#40202024137](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40202024137).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #116 — Adaptive Routing Using Verified Historical Performance [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderRoutingManager` can rank eligible candidates using `ProviderBenchmarkManager` history and exclude low-success/low-sample candidates when confidence-aware routing is enabled. Continuous online adaptation, drift detection, and production experiment validation remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #117 — Region/Data-Residency-Aware Provider Selection [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderProjectPolicyManager` persists a validated `required_region`; routing enforces it and fails closed when provider region metadata is absent. Caller/project region conflicts are rejected. Authoritative region attestations and deployment-level data-residency enforcement remain open. Evidence: `tests/test_project_policy_manager.py::test_required_region_rejects_unsafe_values`, `tests/test_routing_manager.py::test_persisted_project_region_policy_is_enforced_by_router`, run [#40199890961](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40199890961).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #118 — Human Approval for High-Cost or Sensitive Decisions [PARTIAL]

- **Current evidence / gap from canonical ledger:** Paid-usage approval gate exists; high-cost/sensitive decision approval workflow is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### O — Enterprise and Large-Production Resiliency

#### #119 — Multi-Project/Multi-Tenant Provider Policies [PARTIAL]

- **Current evidence / gap from canonical ledger:** New `ProviderTenantPolicyManager` stores tenant-scoped restrictions separately; routing requires a configured tenant policy when enabled and intersects tenant/project provider, model, and capability allowlists, unions deny lists, applies the stricter cost ceiling, preserves restrictive fallback/health settings, and rejects conflicting regions. Tenant-bound principals are checked before service job access and routed generation. Centralized policy administration, authorization around policy mutation, and integration into every generation entry point remain incomplete. Evidence: `tests/test_tenant_policy_manager.py`, `tests/test_routing_manager.py::test_tenant_policy_is_required_and_intersects_project_allowlist`, `tests/test_provider_service.py::test_routed_generation_denies_cross_tenant_principal_before_adapter`, run [#40200591635](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200591635).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #120 — Tenant Isolation for Credentials, Quotas and Job Data [PARTIAL]

- **Current evidence / gap from canonical ledger:** Provider quota reservations enforce tenant-scoped limits; provider jobs require tenant identity for submit/claim/complete/poll/cancel and hide mismatched-tenant records. `ProviderService` checks principal-to-tenant membership before job access and routed adapter invocation, preventing cross-tenant ID probing and scope omission by tenant-bound principals. Credential storage and provider credential retrieval are not yet tenant-isolated, so the full feature remains partial. Evidence: `tests/test_quota_manager.py::ScopedProviderQuotaTests`, `tests/test_provider_job_manager.py::TenantScopedProviderJobTests`, `tests/test_provider_authorization.py::test_tenant_membership_denies_cross_tenant_and_missing_scope`, `tests/test_provider_service.py::test_async_job_facade_propagates_tenant_scope`, run [#40200591635](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200591635).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #121 — Separate User/Project/Tenant Usage Limits [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderQuotaManager` now accepts separate user/project/tenant scoped policies, requires configured scope identities, enforces per-scope hourly/daily/monthly limits, reports scoped usage, rejects malformed scope inputs and cross-scope request-ID reuse. Integration into every provider-generation entry point and policy administration remains incomplete. Evidence: `tests/test_quota_manager.py::ScopedProviderQuotaTests`, run [#40198988980](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40198988980).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #122 — Fair Scheduling and Workload Prioritization [PARTIAL]

- **Current evidence / gap from canonical ledger:** Provider jobs support priority aging to prevent old low-priority jobs starving, plus configurable per-tenant queue and concurrent-worker limits to reduce noisy-neighbor impact. A global fair scheduler across tenants/projects, weighted service classes, and starvation/load benchmarking remain incomplete. Evidence: `tests/test_provider_job_manager.py::test_priority_aging_prevents_old_low_priority_job_starvation`, `TenantScopedProviderJobTests::test_per_tenant_queue_and_concurrency_limits_prevent_noisy_neighbor`, run [#40199371758](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40199371758).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #123 — Provider/Regional Outage Handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** Health and fallback primitives exist; region-wide outage handling is not integrated.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #124 — Disaster Recovery and Restore Procedures [PARTIAL]

- **Current evidence / gap from canonical ledger:** Catalog restore and atomic persistence exist; full disaster recovery/restore drills are not verified.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #125 — Degraded Mode During Partial Service Outages [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `ProviderDegradedModeManager` to classify required capabilities as NORMAL, DEGRADED, OUTAGE, or UNKNOWN using a supplied provider-health snapshot. Malformed/duplicate records fail closed; recommendations preserve tenant, privacy, region, cost, and approval constraints. Live health collection, queue pause/resume, automated rerouting, and recovery orchestration remain incomplete. Evidence: `tests/test_provider_degraded_mode_manager.py`, run [#40201770004](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40201770004).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #126 — Capacity Planning and Load Testing [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added a concurrent queue-load regression that submits 60 jobs, runs eight worker threads against a four-job concurrency cap, and verifies every job completes exactly once; cross-platform CI confirms correctness. Sustained multi-process/device load, throughput/latency baselines, resource profiling, and production capacity planning remain incomplete. Evidence: `tests/test_provider_job_manager.py::test_concurrent_queue_load_completes_every_job_once`, run [#40200733706](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200733706).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #127 — Service-Level Objectives (SLOs) [PARTIAL]

- **Current evidence / gap from canonical ledger:** New `ProviderSLOManager` persists validated provider/category thresholds for minimum success rate, p95 latency, timeout rate, and average queue wait; it distinguishes insufficient samples from compliance and reports each breach. `ProviderService` includes SLO evaluation in execution telemetry when configured. Durable metrics storage, time-windowed SLOs, alert delivery, dashboards, and error-budget operations remain incomplete. Evidence: `tests/test_provider_slo_manager.py`, `tests/test_provider_service.py::test_execution_records_latency_queue_wait_and_failure_metrics`, run [#40201003528](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40201003528).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #128 — Operational Runbooks and Incident Recovery [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `projects/provider_store/PROVIDER_INCIDENT_RUNBOOK.md` covering severity, safe triage, provider degradation/fallback, credential/rate-limit/quota/cost incidents, unknown paid outcomes, tenant/privacy incidents, queue recovery, rollback gates, closure, and required drill evidence. Actual authorized production incident drills and recovery/restore evidence remain outstanding.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.


### P — Future-Ready Extensibility

#### #129 — Plugin-based provider adapters [PARTIAL]

- **Current evidence / gap from canonical ledger:** `projects/generation/providers/provider_manager.py` now validates provider names/class names, restricts module imports to `projects.generation.providers`, rejects traversal/absolute/backslash/non-Python paths and duplicate module entries, stages all instances before swapping the active registry, and preserves the existing registry on failed reload. The loader also requires each instantiated image provider to expose callable `generate_image` before activation. Six regression tests cover trusted loading, traversal rejection, duplicate/invalid entries, atomic failure, stale-provider removal, and missing-interface rejection; all six pass on Ubuntu and Windows in run [#40391867877](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40391867877). Process isolation, signed plugin packages, and full integration with the provider-store catalog remain open.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #131 — Validated metadata کے ساتھ capability discovery [PARTIAL]

- **Current evidence / gap from canonical ledger:** Validated capability metadata and discovery exist; live provider metadata trust is incomplete.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #132 — نئے providers کو محفوظ طریقے سے شامل کرنے کا workflow [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `ProviderOnboardingWorkflow` to validate candidate IDs/capabilities and subscription policy, require official HTTPS docs for API providers, reject credential values and duplicate providers, perform explicit bounded probe verification, and register candidates disabled/LOCKED only after authenticated `provider.configure` authorization. Missing authorization managers and denied principals fail closed. Tests cover malformed metadata/URLs, secret rejection, strict probe outcomes, disabled-by-default registration, duplicate prevention, authorization denial, and real catalog integration. Evidence: `tests/test_provider_onboarding.py`, run [#40446513581](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40446513581). Authorized activation approval and real provider-specific probe integrations remain open. Evidence: `tests/test_provider_onboarding.py`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #133 — Self-hosted اور local models کی support، جہاں ممکن ہو [PARTIAL]

- **Current evidence / gap from canonical ledger:** Added `LocalModelProviderAdapter` for local Ollama `/api/generate` text inference; remote/private-network endpoints, URL credentials, query/fragment injection, redirects and malformed/oversized responses are rejected. Installation discovery, model lifecycle/health checks, other local runtimes/modalities, and live server verification remain incomplete. Evidence: `tests/test_local_model_provider_adapter.py`, run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191), including direct `execute()` provider/request-ID validation.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #134 — Model deprecation اور end-of-life handling [PARTIAL]

- **Current evidence / gap from canonical ledger:** Catalog schema records a validated deprecation reason/date and rejects deprecated providers that are enabled; OpenAI Sora is marked deprecated and disabled. General deprecation discovery, replacement migration, and EOL routing remain open. Evidence: `tests/test_provider_catalogs.py::test_deprecated_provider_requires_reason_date_and_stays_disabled`.
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #137 — Provider configuration اور policy کے لیے admin controls [PARTIAL]

- **Current evidence / gap from canonical ledger:** `ProviderService.set_provider_enabled` and `configure_provider` now require explicit role authorization, fail closed when no authorization manager is configured, and forward paid-usage approval to the connection manager. Viewer-level mutation and authorization-failure regressions pass. Full policy editing, tenant-scoped administration, external identity integration, and management UI remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`, run [#40197348705](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40197348705).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

#### #138 — Documentation، diagnostics and maintenance tooling [PARTIAL]

- **Current evidence / gap from canonical ledger:** `provider_diagnostics.py` performs read-only, bounded-size JSON state checks, rejects duplicate object keys/non-finite numbers, detects duplicate paths, and emits secret-free remediation hints. The actual file read is capped as well as the pre-read size check, preventing file-growth races; path-resolution errors fail closed without exposing raw exception details. Regression tests cover both cases. Full maintenance/repair workflows remain open. Evidence: `tests/test_provider_diagnostics.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Implementation steps:**
  1. Read the existing implementation and tests named in the gap; identify the precise unimplemented end-to-end path.
  2. Close the documented gap without regressing existing verified behavior; integrate the control into every relevant production entry point.
  3. Add regression tests for the missing path plus negative/security/failure cases; add persistence/concurrency tests where applicable.
  4. Run focused tests and full cross-platform CI; only change PARTIAL to VERIFIED when the entire canonical requirement is evidenced. If live credentials or deployment evidence are missing, keep PARTIAL and state the blocker.
- **Acceptance criteria:** End-to-end behavior is exercised; failure paths are safe; tests pass on Ubuntu and Windows; ledger evidence is updated; no unsupported live-verification claim.

## Production/live-evidence blockers — never mark VERIFIED by assumption

These requirements need real environment evidence when applicable: provider-specific API credentials and OAuth consent/scopes; live provider/model catalog and price/quota semantics; actual image/video/audio/music outputs; real local Ollama server/model inference; codec-level decoding; managed secret-vault and rotation; external alert delivery; remote backup/restore; security review; production deployment, rollback and operations runbook. Unit tests with mocks or injected transports are useful but do not prove these live integrations.

## Required completion report after each batch

Report only after making a real commit:
- Batch and ledger item numbers completed/changed.
- Commit SHA and branch.
- Files changed.
- Focused tests and full CI result per OS.
- New ledger counts (VERIFIED / PARTIAL / OPEN) and verified percentage.
- Remaining blockers and next item numbers.
- Explicitly state whether any live provider test was run and what credentials/environment were used (never reveal secrets).

## Owner approval boundary

PR #79 must remain unmerged until the owner explicitly authorizes merge. Do not deploy, spend money, enable paid provider usage, rotate/delete credentials, or perform destructive production actions without explicit approval.
