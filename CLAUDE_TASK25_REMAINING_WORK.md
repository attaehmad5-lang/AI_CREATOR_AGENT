# Claude Code Handoff — Task #25 Remaining Work (138-item ledger)

**Repository:** `attaehmad5-lang/AI_CREATOR_AGENT`  
**Working branch / PR:** `task25-portable-health-gate-fix-20261025` / [PR #79](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/79)  
**Canonical source of truth:** `projects/provider_store/TASK25_CANONICAL_138_ITEM_LEDGER.md`  
**Ledger blob at handoff creation:** `1e0ec2d3c84615716dd933e715f1d061c84f70a7`

This is an actionable work queue for Claude Code or another coding agent. It is not a claim that remaining work is complete.

## Honest progress and the 81% target

- Canonical scope: **138 features**.
- Counts at this ledger revision: **29 VERIFIED, 103 PARTIAL, 6 OPEN**.
- Count a feature as complete only when its ledger status is **VERIFIED**. PARTIAL is not complete.
- To reach at least **81% VERIFIED**, at least **112/138** features must be VERIFIED (81.16%). From this snapshot, **83 additional features** must become VERIFIED. Recalculate after each update.
- Re-check the newest commit's GitHub Actions before reporting status. A previously green run does not verify a newer commit.

## Required working rules

1. Read the canonical ledger, source code, tests and workflow definitions before changing a feature. Preserve all original feature numbers, names, A–P sections and baseline markers.
2. Work in vertical slices: **inspect → implement → focused tests → full regression suite → Windows + Ubuntu CI → review diff → update ledger evidence → commit**. Continue with the next slice after a pass.
3. Prefer security-critical, integration-complete and deterministically verifiable work first. Do not game the percentage by editing labels or only writing documentation.
4. Never commit API keys, OAuth tokens, `.env`, `hf_token.txt`, generated secrets, private artifacts or user prompts. Use secret references and run secret scanning.
5. Never make a real paid provider call without authenticated `billing.approve`, explicit scoped unexpired approval and budget reservation. Unknown outcomes may have incurred charges: retain reservations and do not blindly retry.
6. Fail closed on malformed/unknown metadata, missing health evidence, policy conflicts, missing tenant scope, invalid signatures/configuration or unavailable authorization.
7. Do not invent provider capabilities, free credits, live results, test counts or CI success. Keep mock evidence distinct from live-provider evidence.
8. Integrate with existing ProviderService, canonical catalog, router, authorization/policy managers and job orchestration; avoid duplicate managers and dead-code-only completion.
9. Keep adapters disabled until official docs, required credentials, policy review and tests are available. Use bounded timeouts and reads, TLS verification, redirect rejection and sanitized errors.
10. Do not merge PR #79 or any PR without explicit repository-owner approval. Make reviewable commits on the working branch.
11. After each slice, record exact files, test names, CI run links, verified behavior and unresolved scope in the canonical ledger. Mark VERIFIED only when the complete original requirement is met.
12. If credentials, paid accounts, devices or production infrastructure are unavailable, finish code and mock tests, add a precise manual/live checklist, and keep the item PARTIAL.

## Execution plan

### Phase 1 — Reach 81% VERIFIED honestly
- Convert PARTIAL items to VERIFIED only where the full requirement can be met with code and deterministic tests: catalog/schema consistency, registration and metadata validation, routing constraints, retry/error handling, health/recovery correctness, atomic output safety, authorization, configuration migration/rollback, diagnostics and testing.
- Finish integration so existing managers are enforced by every relevant generation/job entry point. An isolated helper alone is not end-to-end completion.
- Complete test mapping for all 138 items and add missing failure-injection, concurrency, cancellation, privacy, tenant-boundary and regression tests.
- Recompute counts after every commit. Do not claim 81% until at least 112 features are VERIFIED and the latest Windows and Ubuntu CI jobs pass.

### Phase 2 — Provider-specific and production evidence
- Implement only officially documented provider APIs and capabilities actually available to this project.
- Run opt-in live tests for every advertised modality/provider and validate real artifacts. Keep unsupported modalities disabled.
- Complete tenant-isolated credential retrieval, managed secrets, telemetry export, incident drills, disaster recovery/restore drills and operational sign-off.
- Supply every release-readiness evidence field. Keep the release gate blocked while any feature is PARTIAL/OPEN or production evidence is missing.

### Verification protocol
- Follow existing repository CI/test commands and workflow configuration; do not assume a command inconsistent with project setup.
- Run focused tests, then the full regression suite, compilation and catalog/schema validation.
- Inspect actual GitHub Actions jobs for the exact commit SHA; both Ubuntu and Windows must pass.
- Check diff whitespace, inspect the complete diff, scan for secrets, and ensure local credentials/artifacts are not staged.
- Fix failures instead of skipping/suppressing them.
- Update the ledger with the feature number, changed files, exact tests, CI links, verified behavior and limitations.

## Feature-by-feature remaining-work register

The status and gap below are taken from the canonical ledger at the revision above. The next action converts that recorded gap into a concrete implementation/test task; the canonical feature requirement remains authoritative.

### A — Basic Provider Management

#### #1 — Provider Registry [PARTIAL]
- **Recorded gap:** `provider_catalog.json`, `connection_manager.py`, catalog/schema tests. The separate generation runtime now resolves its default registry relative to its module and rejects malformed root/image sections, but the provider-store catalog is not yet the single source of truth for runtime adapter activation; portability/schema regressions pass on Ubuntu and Windows in run [#40391867877](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40391867877).
- **Next action:** Make one validated catalog the source of truth for discovery, activation and runtime adapters. Add schema/version migration, atomic updates, stable IDs, cross-platform path tests and catalog-to-runtime consistency tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #2 — Provider Registration [PARTIAL]
- **Recorded gap:** Registration now validates the complete candidate catalog before persistence, rejecting unsupported auth types and malformed capability metadata without writing the provider; regression tests cover invalid registration and persistence safety. Onboarding and runtime adapter registration remain incomplete. Evidence: `tests/test_connection_manager.py::test_register_provider_rejects_catalog_schema_violations_without_persisting`.
- **Next action:** Complete staged onboarding: official docs, schema/capability validation, secret references only, disabled-by-default registration, authorized probe, approval and explicit activation. Prove failures never persist partial catalog state.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #7 — Provider Metadata [PARTIAL]
- **Recorded gap:** Metadata/schema validation exists; live capability/version/price metadata refresh is incomplete.
- **Next action:** Populate trusted provider/model/version/capability/region/pricing metadata from official sources; reject unknown or contradictory metadata, refresh safely, and test that routing never selects a provider missing required capability/model evidence.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### B — Authentication and Connection

#### #9 — API Key/Token Authentication [PARTIAL]
- **Recorded gap:** Environment-only credential references and bounded HTTP probes exist; provider-specific auth schemes require live verification.
- **Next action:** Define auth per enabled provider from official docs; load secret references only from environment or an approved secret store, fail closed on missing credentials, and test redaction plus 401/403 handling without real secrets.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #10 — OAuth/Login Support [PARTIAL]
- **Recorded gap:** Generic OAuth 2.0 PKCE helper is tested; provider-specific registration, consent and scopes are not live-verified.
- **Next action:** Finish provider-specific OAuth PKCE, exact scopes/state validation, expiry/refresh/revocation and concurrent-refresh behavior. Use injected/mock transport tests; never commit tokens. Keep live consent tests manual and credential-gated.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #11 — Credential Expiry and Refresh [PARTIAL]
- **Recorded gap:** Token expiry/refresh lifecycle primitives exist; OAuth provider lookup errors now fail closed without leaking storage details. Provider-specific refresh semantics and revoked-token recovery remain open. Evidence: `tests/test_provider_service.py::test_oauth_verification_fails_closed_when_provider_lookup_raises`.
- **Next action:** Finish provider-specific OAuth PKCE, exact scopes/state validation, expiry/refresh/revocation and concurrent-refresh behavior. Use injected/mock transport tests; never commit tokens. Keep live consent tests manual and credential-gated.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #12 — Connection and Network Timeouts [PARTIAL]
- **Recorded gap:** Bounded timeout validation and tests exist; full generation cancellation/deadline propagation remains open.
- **Next action:** Propagate a monotonic deadline and cancellation from request through adapter, retry, queue lease and worker shutdown. Ensure uncertain paid outcomes retain reservations, cancelled requests do not retry, cleanup is bounded, and restart recovery is tested.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #13 — Live Connectivity Verification [PARTIAL]
- **Recorded gap:** Generic authenticated HTTPS probe and local Hugging Face check are documented; no live credential test was run in CI.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #15 — TLS/HTTPS/Certificate Verification [PARTIAL]
- **Recorded gap:** HTTPS validation and standard-library TLS verification are used; deployment-specific certificate/pinning review remains open.
- **Next action:** Verify HTTPS-only policy, default certificate and hostname validation, redirect rejection and bounded reads. Add negative tests for HTTP, invalid certificate/hostname and redirects; do not add pinning without a rotation/ownership plan.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #16 — Authentication, Permission and Access Error Handling [PARTIAL]
- **Recorded gap:** Safe 401/403 handling exists; provider-specific permission/scope errors need live verification.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### C — Multimodal Provider Support

#### #17 — Image Generation Providers [PARTIAL]
- **Recorded gap:** Hugging Face and Pollinations are cataloged; this task's manager tests do not prove live generation outputs.
- **Next action:** Run gated live image generation through each enabled image adapter; verify a decodable image, MIME/signature/dimensions, attribution metadata, privacy and cost accounting. Keep mock tests independent of live credentials.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #18 — Video Generation Providers [OPEN]
- **Recorded gap:** No verified working video adapter and end-to-end generation test in this manager scope.
- **Next action:** Implement a real officially documented video adapter behind the existing modality contract; validate authentication, bounded polling/download, timeouts, output integrity and cost approval. Add mock success/error/timeout/cancel tests and a credential-gated live E2E test; require a decodable artifact before VERIFIED.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #19 — Voice/Speech/Audio Providers [OPEN]
- **Recorded gap:** No verified provider-specific voice/audio generation adapter and live test.
- **Next action:** Add an officially documented speech/audio adapter with explicit supported task schemas, safe auth, bounded timeouts, output validation and sanitized errors. Test success, malformed responses, timeout, permission denial and cancellation; separate mock tests from credential-gated live E2E.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #20 — Text/Script/Language Model Providers [PARTIAL]
- **Recorded gap:** Added an Ollama-compatible local text adapter with numeric-loopback-IP-only endpoints, bounded timeouts/response size, explicit `done: true` completion validation, and no prompt leakage in error records. `ProviderService` validates and allowlists adapter results. Regression tests pass on Ubuntu/Windows in run [#40364204249](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40364204249). Provider-specific cloud text/script adapters and real local-model end-to-end verification remain incomplete.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #21 — Music Generation Providers [OPEN]
- **Recorded gap:** No verified music generation adapter and output validation test.
- **Next action:** Implement a documented music-generation adapter with asynchronous job polling if required, bounded timeouts, artifact validation and paid-use approval. Add deterministic mock tests and a credential-gated live output test; do not fabricate live evidence.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #22 — Image Editing/Upscaling/Restoration [OPEN]
- **Recorded gap:** No verified end-to-end editing/upscaling/restoration adapters.
- **Next action:** Implement true image-to-image editing/upscale/restore operations rather than aliasing plain generation. Validate input and output decoding, dimensions, size, atomic replacement and preservation of previous good output; test all failure and cleanup paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #23 — Transcription, Translation and Dubbing [OPEN]
- **Recorded gap:** No verified end-to-end transcription/translation/dubbing adapters.
- **Next action:** Implement separately typed task contracts for transcription, translation and dubbing; validate formats, language metadata, timestamps/subtitles where applicable, privacy and cost approval. Test every advertised task independently.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #24 — Future Modality Support and Contract Tests [PARTIAL]
- **Recorded gap:** Capability identifiers and callable modality contracts are tested; working adapters for future modalities remain open.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### D — Routing and Selection

#### #30 — Capability-Based Routing [PARTIAL]
- **Recorded gap:** Capability filtering exists and is tested at discovery; the complete canonical routing/metadata truth remains incomplete.
- **Next action:** Populate trusted provider/model/version/capability/region/pricing metadata from official sources; reject unknown or contradictory metadata, refresh safely, and test that routing never selects a provider missing required capability/model evidence.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #31 — Model/Version-Aware Routing [PARTIAL]
- **Recorded gap:** Router and catalog schema now support declared model IDs/versions and fail closed when metadata is absent; live model catalogs are not populated for enabled providers.
- **Next action:** Populate trusted provider/model/version/capability/region/pricing metadata from official sources; reject unknown or contradictory metadata, refresh safely, and test that routing never selects a provider missing required capability/model evidence.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #32 — User/Project Policy [PARTIAL]
- **Recorded gap:** Project IDs and policy gates exist; complete user/project policy enforcement is not verified.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### E — Retry, Failure and Recovery

#### #34 — Error-Specific Retry Policy [PARTIAL]
- **Recorded gap:** Retryability/status mapping exists; provider-specific transient failure coverage is incomplete.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #37 — Quota Exhaustion Detection [PARTIAL]
- **Recorded gap:** Quota manager and routing rejection paths are tested; provider-side quota semantics remain incomplete.
- **Next action:** Use official provider quota/balance semantics and distinguish exhausted credit from transient throttling. Never infer free credits from marketing text; add sanitized mapping tests, hard budget stops and authorized live checks where possible.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #39 — Cooldown and Provider Recheck [PARTIAL]
- **Recorded gap:** `ProviderRecoveryCoordinator` schedules explicit rechecks only for due UNHEALTHY/DEGRADED providers, enforces cooldown, rejects future/malformed timestamps, fails closed on corrupt snapshots, and avoids generation/routing side effects. `ProviderService.run_due_provider_rechecks()` exposes the explicit operation and now checks `provider.verify` authorization before coordinator invocation whenever an authorization manager is configured; denied requests are sanitized and do not trigger rechecks. Regression coverage: `tests/test_recovery_coordinator.py` and `tests/test_provider_service.py`, including denied/allowed authorization. Snapshot parsing rejects duplicate keys/non-finite numbers, bounds actual reads to 2 MiB, validates timezone-aware clocks, and rejects huge cooldown values without overflow. Full background scheduling and production deployment integration remain open.
- **Next action:** Connect rechecks to a bounded scheduler with cooldown, authorization, concurrency protection and persistence. Test corrupt/stale/future evidence, recovery thresholds, restart and no generation side effects during probes.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #40 — Timeout, Cancellation and Stalled-Request Handling [PARTIAL]
- **Recorded gap:** Timeout handling is tested; cancellation, deadlines and stalled-job recovery are incomplete.
- **Next action:** Propagate a monotonic deadline and cancellation from request through adapter, retry, queue lease and worker shutdown. Ensure uncertain paid outcomes retain reservations, cancelled requests do not retry, cleanup is bounded, and restart recovery is tested.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### F — Health and Reliability

#### #43 — Health Check Before Generation [PARTIAL]
- **Recorded gap:** `ProviderService.execute_routed_request(..., require_fresh_health_check=True)` now checks recorded eligibility before adapter invocation and fails closed on missing/stale/recovering/unhealthy/slow/malformed evidence or an unavailable manager; tests cover blocked unknown health and permitted fresh evidence. A separate opt-in gate now also protects `ProviderService.generate_local_text()` and validates an explicit provider ID; missing, stale, malformed, or exception-producing health evidence blocks adapter invocation. Latest local-text health-gate regression evidence: run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191). Both paths remain opt-in, use stored evidence rather than running a live probe, and are not yet enforced across every generation entry point. Evidence: `tests/test_provider_service.py::test_routed_generation_can_require_fresh_health_evidence`, run [#40379755049](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40379755049).
- **Next action:** Enforce health eligibility consistently across every generation entry point (routed, local, async and modality paths). Fail closed on missing/stale/RECOVERING evidence and prove blocked paths never invoke adapters.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #45 — Active Health Probes [PARTIAL]
- **Recorded gap:** Generic HTTPS probe exists; provider-specific authenticated probes and live results remain open.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #47 — Provider Recovery Detection [PARTIAL]
- **Recorded gap:** `ProviderHealthManager` supports a configurable consecutive-success recovery threshold; providers recovering from `UNHEALTHY` remain ineligible as `RECOVERING` until enough successful probes are recorded. Default threshold preserves existing one-success behavior; full automated probe scheduling remains open.
- **Next action:** Connect rechecks to a bounded scheduler with cooldown, authorization, concurrency protection and persistence. Test corrupt/stale/future evidence, recovery thresholds, restart and no generation side effects during probes.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #48 — Stale Health and Consistency Checks [PARTIAL]
- **Recorded gap:** Stale/future health evidence is excluded, malformed persisted-state checks fail closed, and `ProviderHealthManager.audit_consistency()` now performs a read-only audit for conflicting status/counters, impossible recovery state, future timestamps, invalid latency, and non-chronological history. Five regression tests cover consistent empty state, counter conflicts, threshold mismatch, future timestamps without repair, and history ordering; run [#40446263623](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40446263623) passes 627 tests on Ubuntu and Windows. Full automated health reconciliation, scheduled re-probing, and safe repair workflows remain open.
- **Next action:** Connect rechecks to a bounded scheduler with cooldown, authorization, concurrency protection and persistence. Test corrupt/stale/future evidence, recovery thresholds, restart and no generation side effects during probes.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### G — Output and Asset Safety

#### #49 — Image Output Validation [PARTIAL]
- **Recorded gap:** Signature/basic file validation is tested; real provider images and decoder-level validation remain open.
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #51 — Audio/Video Format, Codec and Integrity Validation [PARTIAL]
- **Recorded gap:** `media_integrity_validator.py` validates WAV frame/data integrity and bounded MP4/WebM/Ogg structure; MP4/WebM/Ogg reads now cap actual bytes read and reject files that grow or change size after the initial stat. `ProviderService` can reject malformed audio/video containers after basic output validation. Full codec decoding, codec allowlists, and semantic media validation remain open. Evidence: `tests/test_media_integrity_validator.py`, run [#40486096062](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40486096062).
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #52 — Corrupt/Incomplete Output Rejection [PARTIAL]
- **Recorded gap:** Basic corrupt/truncated artifact rejection is tested; decoder-level detection remains open.
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #53 — Temporary File Cleanup and Atomic Replacement [PARTIAL]
- **Recorded gap:** Atomic replacement helpers exist; output-pipeline cleanup integration is incomplete.
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #54 — Previous Output Preservation on Failure [PARTIAL]
- **Recorded gap:** Some preservation/atomicity tests exist; end-to-end generation replacement behavior is not verified.
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #55 — File Size, Dimensions, Duration and Codec Constraints [PARTIAL]
- **Recorded gap:** Size limits exist; dimensions/duration/codec constraints need modality-specific decoders.
- **Next action:** Integrate validators and atomic temp-file replacement into every output writer. Enforce size/dimension/duration/codec bounds, decode media where supported, clean temp files on every failure, and prove an existing good output remains byte-identical after failed replacement.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #56 — Provider Attribution and Output Metadata [PARTIAL]
- **Recorded gap:** Request/provider IDs exist in service records; complete asset lineage metadata is not verified.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### H — Cost, Free Credits and Quotas

#### #57 — Free-Credit-First Routing [PARTIAL]
- **Recorded gap:** Routing and credit reservations exist; provider-specific verified free-credit balances are still required.
- **Next action:** Use official provider quota/balance semantics and distinguish exhausted credit from transient throttling. Never infer free credits from marketing text; add sanitized mapping tests, hard budget stops and authorized live checks where possible.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #58 — Provider Free-Tier/Quota Verification [PARTIAL]
- **Recorded gap:** Generic JSON account-balance probe exists; provider-specific documented fields and authorized live checks are missing.
- **Next action:** Use official provider quota/balance semantics and distinguish exhausted credit from transient throttling. Never infer free credits from marketing text; add sanitized mapping tests, hard budget stops and authorized live checks where possible.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #59 — Explicit Approval Before Paid Usage [PARTIAL]
- **Recorded gap:** Paid routing now rejects a non-empty caller-supplied `paid_approved_providers` set unless `paid_usage_approved=True` is explicitly supplied and the authenticated principal passes `billing.approve`; a forged provider set alone cannot authorize spending. Regression tests cover forged-set rejection and missing billing permission. Durable approval requests, approver/requester separation, expiry, and revocation remain incomplete.
- **Next action:** Implement durable approvals bound to requester/approver, provider/model, maximum cost, project/tenant, expiry and revocation. Enforce role separation and re-check at execution time; planning remains side-effect-free and no paid call starts without valid approval.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #60 — Per-Request/Project Budgets [PARTIAL]
- **Recorded gap:** Project budget and quota managers are tested; end-to-end tenant/user budgeting is incomplete.
- **Next action:** Integrate user/project/tenant hard limits, reserve before dispatch, reconcile provider-reported actual usage, and retain reservations for unknown outcomes. Test under/over-estimates, duplicate callbacks, billing failure and no-spend blocked paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #61 — Estimated vs Actual Usage Reconciliation [PARTIAL]
- **Recorded gap:** Cost reservation/reconciliation is implemented and tested; provider-specific actual usage adapters are missing.
- **Next action:** Integrate user/project/tenant hard limits, reserve before dispatch, reconcile provider-reported actual usage, and retain reservations for unknown outcomes. Test under/over-estimates, duplicate callbacks, billing failure and no-spend blocked paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #62 — Credit Exhaustion and Billing Error Handling [PARTIAL]
- **Recorded gap:** `provider_error_policy.py` normalizes explicit billing/credit/auth/permission/rate-limit/transient signals and recognizes additional credit/budget aliases (`insufficient_balance`, `no_credits_remaining`, `credit_limit_exceeded`, `monthly_budget_exceeded`, `billing_limit_exceeded`) plus billing-state aliases; focused tests assert these never retry or expose raw provider codes. `ProviderService.execute_routed_request` blocks retry for non-retryable billing/credit outcomes, including HTTP 429 with `insufficient_quota`. Provider-specific official mappings and authorized live account tests remain open.
- **Next action:** Use official provider quota/balance semantics and distinguish exhausted credit from transient throttling. Never infer free credits from marketing text; add sanitized mapping tests, hard budget stops and authorized live checks where possible.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #63 — Cost-Aware Provider/Model Selection [PARTIAL]
- **Recorded gap:** Cost/credit-aware routing foundations exist; reliable live prices and model-level cost comparisons remain open.
- **Next action:** Integrate user/project/tenant hard limits, reserve before dispatch, reconcile provider-reported actual usage, and retain reservations for unknown outcomes. Test under/over-estimates, duplicate callbacks, billing failure and no-spend blocked paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #64 — Budget Alerts and Hard-Stop Policy [PARTIAL]
- **Recorded gap:** `BudgetAlertManager` persists idempotent 50/80/100% threshold events, keeps failed deliveries pending with bounded attempt metadata, and supports injected notification dispatch with retry-safe outcomes. External channel setup, scheduling, and end-to-end delivery monitoring remain open.
- **Next action:** Integrate user/project/tenant hard limits, reserve before dispatch, reconcile provider-reported actual usage, and retain reservations for unknown outcomes. Test under/over-estimates, duplicate callbacks, billing failure and no-spend blocked paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### I — Security, Privacy and Governance

#### #65 — Secrets Manager Integration [PARTIAL]
- **Recorded gap:** Environment variable references are supported; managed secret vault/rotation integration is not complete.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #66 — Secret/Token Leak Prevention in Logs [PARTIAL]
- **Recorded gap:** Credential redaction and no-response-body probes are tested; full application log scan is pending.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #67 — Least-Privilege Access Control [PARTIAL]
- **Recorded gap:** `provider_authorization.py` persists explicit viewer/creator/operator/billing-manager/admin role assignments and denies unknown principals/operations; `ProviderService` checks generation and verification permissions, authorizes provider enable/disable/configuration management, and separately requires `billing.approve` for paid activation. Tenant-bound resource checks, broader management endpoints, and end-to-end identity-provider integration remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #68 — Provider Permissions and Allowed Operations [PARTIAL]
- **Recorded gap:** Explicit role-to-operation permissions are persisted; `ProviderService` checks `provider.generate` before execution, `provider.verify` before probes, and `provider.enable`/`provider.disable`/`provider.configure` before management operations. Provider-specific scopes and authorization for other management endpoints remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #69 — Sensitive-Data Detection and Privacy Enforcement [PARTIAL]
- **Recorded gap:** `sensitive_data_policy.py` detects credential-like tokens, email addresses and phone numbers with bounded traversal, returns only categories/paths (never matched values), and blocks configured sensitive categories before `ProviderService` routes or invokes an adapter. Broader data-at-rest/retention enforcement and end-to-end privacy controls remain open.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #70 — Data Retention and Provider Data-Handling Rules [PARTIAL]
- **Recorded gap:** `provider_data_policy.py` requires explicit HTTPS-sourced provider terms, allowed data categories, maximum retention, and training-use permission/consent; routed requests are blocked when a configured policy is missing or rejects the request. Local text generation now also passes an explicit provider ID, categories, integer retention period, provider-training flag, and user consent through the policy gate and fails closed on denial/exception; latest local-text integration regressions pass in run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191). Provider-specific verified policy catalog coverage, mandatory policy-manager configuration, and downstream deletion/retention enforcement remain open.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #71 — Provider Endpoint Allowlisting and Network Security [PARTIAL]
- **Recorded gap:** HTTPS host allowlists, redirect rejection and bounded reads are tested; deployment threat-model review remains open.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #72 — Routing, Credential and Approval Audit Logs [PARTIAL]
- **Recorded gap:** Provider activation decisions now append structured, secret-free JSONL audit events with a SHA-256 hash chain under a cross-process lock; allowed/denied decisions, tampering, and audit-write failure blocking are tested. Full audit coverage for routing/credential/approval events, retention/rotation, external tamper anchoring, and distributed trace linkage remain incomplete. Evidence: `ConnectionManager._append_audit_event`, `tests/test_connection_manager.py::ProviderPolicySimulationTests`, run [#40197656173](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40197656173).
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### J — Production Orchestration

#### #73 — Unique Request ID and Trace ID [PARTIAL]
- **Recorded gap:** Request IDs are now restricted to 1–128 safe ASCII characters before routing/reservation; `execute_routed_request` also validates optional trace IDs and propagates them to adapters and returned execution outcomes. Full distributed trace collection remains incomplete; untrusted trace IDs hidden in adapter options are discarded. Cross-service propagation through remote jobs, webhooks, and external telemetry remains incomplete. Evidence: `tests/test_provider_service.py::test_execution_records_latency_queue_wait_and_failure_metrics`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #74 — Idempotency and Duplicate Prevention [PARTIAL]
- **Recorded gap:** Manager-level idempotency is tested; full external-provider request idempotency is not verified.
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #75 — Concurrency and Provider-Specific Worker Limits [PARTIAL]
- **Recorded gap:** Cross-process reservations and locking are tested; worker pools and provider-specific concurrency limits are incomplete.
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #76 — Queue, Backpressure and Overload Handling [PARTIAL]
- **Recorded gap:** `provider_job_manager.py` implements a persistent bounded queue, priority aging, concurrency limits, backpressure retry hints, idempotent submission, lease recovery, and cancellation-aware lease renewal. `ProviderService.renew_job_lease()` applies tenant membership and `provider.generate` authorization before renewal. Invalid/missing/naive leases fail safe; expired workers cannot commit stale results or overwrite a retry. Tests cover stale completion, malformed leases, renewal ownership/expiry/cancellation, and cross-tenant denial. Production distributed queue scaling and sustained load tests remain open. Evidence: `tests/test_provider_job_manager.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #77 — Async/Long-Running Job and Polling Support [PARTIAL]
- **Recorded gap:** Persistent `QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED` states, polling, worker leases, bounded retries, cancellation, result references, and live lease renewal are tested. Expired workers are rejected even before a retry is claimed; corrupt/missing leases are reaped safely, and renewal is gated by ownership, tenant authorization, and cancellation state. Provider-specific remote job polling/webhook orchestration remains open. Evidence: `tests/test_provider_job_manager.py`, `tests/test_provider_service.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #78 — Webhook/Callback Verification [PARTIAL]
- **Recorded gap:** `provider_webhook_verifier.py` validates HMAC-SHA256 signatures with constant-time comparison, bounded timestamps/payloads, and persistent event replay rejection; `ProviderService.verify_webhook` delegates without echoing secrets. Provider-specific header/signature conventions and production callback ingress integration remain open.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #79 — Cancellation, Deadlines and Graceful Shutdown [PARTIAL]
- **Recorded gap:** Cancellation requests are honored during lease recovery and prevent lease renewal; authorized workers can renew a still-live lease. End-to-end cancellation/deadline propagation and graceful worker-pool shutdown remain incomplete. Evidence: `tests/test_provider_job_manager.py`, `tests/test_provider_service.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Propagate a monotonic deadline and cancellation from request through adapter, retry, queue lease and worker shutdown. Ensure uncertain paid outcomes retain reservations, cancelled requests do not retry, cleanup is bounded, and restart recovery is tested.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #80 — Generation State Manager/Orchestration Integration Tests [PARTIAL]
- **Recorded gap:** Cross-component restart recovery remains tested, and the underlying queue now rejects expired worker completion and safely reaps malformed leases. Broader Generation State Manager/Executor/Orchestrator/Retry Manager integration and crash-recovery matrices remain incomplete; Task #29 is not declared complete. Evidence: `tests/test_provider_service.py::test_job_state_recovers_expired_worker_lease_across_service_restart`, `tests/test_provider_job_manager.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### K — Quality, Performance and Observability

#### #82 — Provider Success/Error-Rate Metrics [PARTIAL]
- **Recorded gap:** `provider_metrics_manager.py` records success/failure/timeout counters and success rate from provider executions; bounded history avoids payload storage. Metrics collection now rejects failing clocks, naive/unsupported timestamps, and durations exceeding seven days so invalid values cannot poison aggregates. Export to production telemetry remains open.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #83 — Throughput, Queue-Wait and Timeout Metrics [PARTIAL]
- **Recorded gap:** `ProviderMetricsManager` aggregates bounded latency percentiles, queue-wait averages, timeout counts, and sample throughput; `ProviderService` records execution latency and queue wait. External telemetry export and load-tested throughput metrics remain open.
- **Next action:** Propagate a monotonic deadline and cancellation from request through adapter, retry, queue lease and worker shutdown. Ensure uncertain paid outcomes retain reservations, cancelled requests do not retry, cleanup is bounded, and restart recovery is tested.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #84 — Output Quality Validation/Score Integration [PARTIAL]
- **Recorded gap:** `provider_quality_policy.py` adds an injected output scorer and `ProviderService` applies its result after file validation; below-threshold or invalid scores prevent an output from being reported successful. A production semantic scorer and modality-specific QA remain open.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #86 — Provider/Model Benchmarking [PARTIAL]
- **Recorded gap:** `provider_benchmark_manager.py` aggregates bounded provider/model samples by success rate, p50/p95 latency, cost per attempt/success, and optional quality score; `ProviderService` can record execution samples when a model ID is available. Controlled benchmark suites, sample-size confidence, and live provider comparisons remain open.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #87 — Structured Logs, Metrics and Distributed Traces [PARTIAL]
- **Recorded gap:** Some structured status metadata exists; end-to-end metrics/tracing is incomplete.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #88 — Alerts, Dashboards and Incident Diagnostics [PARTIAL]
- **Recorded gap:** `provider_incident_manager.py` persists deduplicated provider incidents with severity escalation, occurrence counts, acknowledgement/resolution, and safe status summaries; `ProviderService` opens critical incidents for explicit billing/credit exhaustion and records other execution failures. Dashboard and external alert delivery remain open.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### L — Configuration, Compatibility and Change Control

#### #90 — Safe Defaults and Invalid-Configuration Handling [PARTIAL]
- **Recorded gap:** Fail-closed validation exists; complete configuration recovery/default audit remains open.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #91 — Configuration Versioning and Migration [PARTIAL]
- **Recorded gap:** `provider_config_lifecycle.py` requires explicit version-to-version migration functions, validates each intermediate configuration, and writes migrated JSON via atomic replacement with a backup. Repository-wide migration coverage and cross-process crash-injection tests remain open.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #92 — Safe Hot Reload [PARTIAL]
- **Recorded gap:** `ProviderConfigLifecycleManager` validates candidate configuration before swapping active state, preserves the last-known-good configuration, and supports rollback after reload failures. Integration into every runtime catalog consumer remains open.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #93 — Feature Flags and Controlled Rollout [PARTIAL]
- **Recorded gap:** `provider_feature_flags.py` persists kill switches, project allow/deny lists, and deterministic request-scoped percentage rollout; `ProviderService` blocks generation when `provider_generation` is disabled or outside rollout. Admin authorization/audit integration and cross-service rollout coordination remain open.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #94 — Provider API/Model Version Compatibility [PARTIAL]
- **Recorded gap:** Model/endpoint validation exists; live API/model version compatibility matrix is incomplete.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #95 — Rollback to Known-Good Configuration [PARTIAL]
- **Recorded gap:** Catalog restore exists; runtime policy rollback is not fully verified.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #96 — Configuration Backup/Restore Verification [PARTIAL]
- **Recorded gap:** Master catalog backup/restore is tested; full config/state backup verification is incomplete.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### M — Testing, Audit and Release Readiness

#### #97 — Routing/Helper Unit Tests [PARTIAL]
- **Recorded gap:** 559-test cross-platform suite covers helper/routing paths; exact item-level coverage matrix remains incomplete.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #101 — Failure/Success Simulation Coverage [PARTIAL]
- **Recorded gap:** Mock success/failure and persistence-failure tests exist; provider-specific network injection remains open.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #102 — Invalid Output, Cleanup and File-Preservation Tests [PARTIAL]
- **Recorded gap:** Output validation tests exist; cleanup/preservation across all generation writers is incomplete.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #103 — Concurrency, Idempotency and Cancellation Tests [PARTIAL]
- **Recorded gap:** Cross-process manager and idempotency tests exist; cancellation and full job orchestration are incomplete.
- **Next action:** Propagate a monotonic deadline and cancellation from request through adapter, retry, queue lease and worker shutdown. Ensure uncertain paid outcomes retain reservations, cancelled requests do not retry, cleanup is bounded, and restart recovery is tested.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #104 — Security, Secret-Leak and Privacy Tests [PARTIAL]
- **Recorded gap:** Credential redaction/endpoint tests exist; full privacy and secret scanning remains open.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #105 — Cost Policy and Paid-Approval Tests [PARTIAL]
- **Recorded gap:** Cost/budget/approval tests pass; provider billing reconciliation remains open.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #106 — Every Supported Modality End-to-End Tests [OPEN]
- **Recorded gap:** Real end-to-end tests for all modalities do not exist yet.
- **Next action:** Build a modality-by-modality E2E matrix. For every advertised supported modality, run a credential-gated live request and verify the actual artifact with a decoder/player or schema validator. Record provider/model, date, checksums and sanitized result; do not claim unsupported modalities.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #107 — Failure Injection and Recovery Tests [PARTIAL]
- **Recorded gap:** Persistence/network error cases are tested; comprehensive cross-manager recovery injection remains open.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #108 — Full Regression Suite and Reproducible Test Reports [PARTIAL]
- **Recorded gap:** 559 tests pass on Windows and Ubuntu, with JSON report artifacts uploaded for both operating systems; complete 138-item coverage mapping and production evidence remain open.
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #109 — Git Commit, Secret-Exclusion Check and Remote Backup Verification [PARTIAL]
- **Recorded gap:** Changes are committed and reviewed in PR #7; dedicated secret scanning and backup verification are not complete.
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #110 — Production Release Checklist and Final PASS [PARTIAL]
- **Recorded gap:** Added `ProviderReleaseReadiness` to fail closed unless all 138 features are VERIFIED, PARTIAL/OPEN counts are zero, and explicit evidence is supplied for cross-platform tests, every modality E2E, live credentials, secret scan, remote backup, restore/rollback, security review, and approved operations runbook. Current ledger still has incomplete features, and live multimodal E2E/production evidence is not complete; no final PASS is claimed. Evidence: `tests/test_provider_release_readiness.py`, run [#40202197447](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40202197447).
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### N — High-Advanced Routing Intelligence

#### #111 — Multi-Factor Routing: Quality, Latency, Cost and Capability [PARTIAL]
- **Recorded gap:** Cost/health/capability foundations exist; unified quality-aware scoring is incomplete.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #112 — Project-Specific Provider Selection [PARTIAL]
- **Recorded gap:** Project identifiers and policies exist; full per-project selection policy is incomplete.
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #113 — Automatic Exclusion of Providers That Cannot Meet Constraints [PARTIAL]
- **Recorded gap:** Capability/health/credit filtering exists; all constraint classes are not covered.
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #114 — Confidence-Aware Routing and Uncertainty Handling [PARTIAL]
- **Recorded gap:** `ProviderRoutingManager.route(confidence_aware=True)` requires model-specific benchmark evidence, configurable minimum sample count/success rate/optional p95 latency, and either explicitly allowed deterministic fallback or fail-closed `CONFIDENCE_INSUFFICIENT`. Confidence-aware route decisions expose bounded benchmark evidence. Calibration and production confidence intervals remain open.
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #115 — Controlled A/B Benchmarking Without Unintended Paid Usage [PARTIAL]
- **Recorded gap:** Added `ProviderABBenchmarkManager` to build side-effect-free A/B plans, default to zero-cost candidates, reject duplicate/malformed candidates, calculate total estimated cost across repetitions, enforce the total budget, and require paid-use opt-in plus a matching approval record. `ProviderService.plan_ab_benchmark()` checks the authenticated principal for `billing.approve` and discards caller-supplied approval objects; the planner never calls providers. An authorized execution runner, real provider cost reconciliation, and controlled live A/B comparison remain incomplete. Evidence: `tests/test_provider_ab_benchmark_manager.py`, `tests/test_provider_service.py::test_ab_benchmark_paid_usage_requires_authenticated_billing_approval`, run [#40202024137](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40202024137).
- **Next action:** Implement durable approvals bound to requester/approver, provider/model, maximum cost, project/tenant, expiry and revocation. Enforce role separation and re-check at execution time; planning remains side-effect-free and no paid call starts without valid approval.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #116 — Adaptive Routing Using Verified Historical Performance [PARTIAL]
- **Recorded gap:** `ProviderRoutingManager` can rank eligible candidates using `ProviderBenchmarkManager` history and exclude low-success/low-sample candidates when confidence-aware routing is enabled. Continuous online adaptation, drift detection, and production experiment validation remain open.
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #117 — Region/Data-Residency-Aware Provider Selection [PARTIAL]
- **Recorded gap:** `ProviderProjectPolicyManager` persists a validated `required_region`; routing enforces it and fails closed when provider region metadata is absent. Caller/project region conflicts are rejected. Authoritative region attestations and deployment-level data-residency enforcement remain open. Evidence: `tests/test_project_policy_manager.py::test_required_region_rejects_unsafe_values`, `tests/test_routing_manager.py::test_persisted_project_region_policy_is_enforced_by_router`, run [#40199890961](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40199890961).
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #118 — Human Approval for High-Cost or Sensitive Decisions [PARTIAL]
- **Recorded gap:** Paid-usage approval gate exists; high-cost/sensitive decision approval workflow is incomplete.
- **Next action:** Implement durable approvals bound to requester/approver, provider/model, maximum cost, project/tenant, expiry and revocation. Enforce role separation and re-check at execution time; planning remains side-effect-free and no paid call starts without valid approval.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### O — Enterprise and Large-Production Resiliency

#### #119 — Multi-Project/Multi-Tenant Provider Policies [PARTIAL]
- **Recorded gap:** New `ProviderTenantPolicyManager` stores tenant-scoped restrictions separately; routing requires a configured tenant policy when enabled and intersects tenant/project provider, model, and capability allowlists, unions deny lists, applies the stricter cost ceiling, preserves restrictive fallback/health settings, and rejects conflicting regions. Tenant-bound principals are checked before service job access and routed generation. Centralized policy administration, authorization around policy mutation, and integration into every generation entry point remain incomplete. Evidence: `tests/test_tenant_policy_manager.py`, `tests/test_routing_manager.py::test_tenant_policy_is_required_and_intersects_project_allowlist`, `tests/test_provider_service.py::test_routed_generation_denies_cross_tenant_principal_before_adapter`, run [#40200591635](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200591635).
- **Next action:** Complete tenant scoping for credentials, policies, quotas, jobs, artifacts and audit records. Require principal-to-tenant membership on every operation; test missing scope, forged IDs, cross-tenant reads/writes and credential retrieval.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #120 — Tenant Isolation for Credentials, Quotas and Job Data [PARTIAL]
- **Recorded gap:** Provider quota reservations enforce tenant-scoped limits; provider jobs require tenant identity for submit/claim/complete/poll/cancel and hide mismatched-tenant records. `ProviderService` checks principal-to-tenant membership before job access and routed adapter invocation, preventing cross-tenant ID probing and scope omission by tenant-bound principals. Credential storage and provider credential retrieval are not yet tenant-isolated, so the full feature remains partial. Evidence: `tests/test_quota_manager.py::ScopedProviderQuotaTests`, `tests/test_provider_job_manager.py::TenantScopedProviderJobTests`, `tests/test_provider_authorization.py::test_tenant_membership_denies_cross_tenant_and_missing_scope`, `tests/test_provider_service.py::test_async_job_facade_propagates_tenant_scope`, run [#40200591635](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200591635).
- **Next action:** Apply least privilege and fail-closed behavior at every entry point; use secret references, redact logs/errors, enforce data category/retention/training consent and endpoint allowlists. Add adversarial cross-tenant, malformed-input, log-scan and secret-scan tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #121 — Separate User/Project/Tenant Usage Limits [PARTIAL]
- **Recorded gap:** `ProviderQuotaManager` now accepts separate user/project/tenant scoped policies, requires configured scope identities, enforces per-scope hourly/daily/monthly limits, reports scoped usage, rejects malformed scope inputs and cross-scope request-ID reuse. Integration into every provider-generation entry point and policy administration remains incomplete. Evidence: `tests/test_quota_manager.py::ScopedProviderQuotaTests`, run [#40198988980](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40198988980).
- **Next action:** Complete tenant scoping for credentials, policies, quotas, jobs, artifacts and audit records. Require principal-to-tenant membership on every operation; test missing scope, forged IDs, cross-tenant reads/writes and credential retrieval.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #122 — Fair Scheduling and Workload Prioritization [PARTIAL]
- **Recorded gap:** Provider jobs support priority aging to prevent old low-priority jobs starving, plus configurable per-tenant queue and concurrent-worker limits to reduce noisy-neighbor impact. A global fair scheduler across tenants/projects, weighted service classes, and starvation/load benchmarking remain incomplete. Evidence: `tests/test_provider_job_manager.py::test_priority_aging_prevents_old_low_priority_job_starvation`, `TenantScopedProviderJobTests::test_per_tenant_queue_and_concurrency_limits_prevent_noisy_neighbor`, run [#40199371758](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40199371758).
- **Next action:** Exercise concurrent and restart scenarios across service, job manager and orchestrator. Enforce bounded queues/leases/retries, stable idempotency keys, cancellation/deadline propagation and tenant fairness; assert each job has one terminal state and no cross-scope leakage.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #123 — Provider/Regional Outage Handling [PARTIAL]
- **Recorded gap:** Health and fallback primitives exist; region-wide outage handling is not integrated.
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #124 — Disaster Recovery and Restore Procedures [PARTIAL]
- **Recorded gap:** Catalog restore and atomic persistence exist; full disaster recovery/restore drills are not verified.
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #125 — Degraded Mode During Partial Service Outages [PARTIAL]
- **Recorded gap:** Added `ProviderDegradedModeManager` to classify required capabilities as NORMAL, DEGRADED, OUTAGE, or UNKNOWN using a supplied provider-health snapshot. Malformed/duplicate records fail closed; recommendations preserve tenant, privacy, region, cost, and approval constraints. Live health collection, queue pause/resume, automated rerouting, and recovery orchestration remain incomplete. Evidence: `tests/test_provider_degraded_mode_manager.py`, run [#40201770004](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40201770004).
- **Next action:** Implement the policy in one routing decision path with explainable sanitized evidence; intersect tenant/project/user restrictions with capability/model/region/privacy/cost/health constraints and fail closed on missing evidence. Add conflict/fallback tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #126 — Capacity Planning and Load Testing [PARTIAL]
- **Recorded gap:** Added a concurrent queue-load regression that submits 60 jobs, runs eight worker threads against a four-job concurrency cap, and verifies every job completes exactly once; cross-platform CI confirms correctness. Sustained multi-process/device load, throughput/latency baselines, resource profiling, and production capacity planning remain incomplete. Evidence: `tests/test_provider_job_manager.py::test_concurrent_queue_load_completes_every_job_once`, run [#40200733706](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40200733706).
- **Next action:** Map this feature to exact tests and CI artifacts; add missing failure injection and cross-platform coverage, run the full suite on Windows and Ubuntu, scan for secrets, verify remote backup/restore, and update evidence only from actual results.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #127 — Service-Level Objectives (SLOs) [PARTIAL]
- **Recorded gap:** New `ProviderSLOManager` persists validated provider/category thresholds for minimum success rate, p95 latency, timeout rate, and average queue wait; it distinguishes insufficient samples from compliance and reports each breach. `ProviderService` includes SLO evaluation in execution telemetry when configured. Durable metrics storage, time-windowed SLOs, alert delivery, dashboards, and error-budget operations remain incomplete. Evidence: `tests/test_provider_slo_manager.py`, `tests/test_provider_service.py::test_execution_records_latency_queue_wait_and_failure_metrics`, run [#40201003528](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40201003528).
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #128 — Operational Runbooks and Incident Recovery [PARTIAL]
- **Recorded gap:** Added `projects/provider_store/PROVIDER_INCIDENT_RUNBOOK.md` covering severity, safe triage, provider degradation/fallback, credential/rate-limit/quota/cost incidents, unknown paid outcomes, tenant/privacy incidents, queue recovery, rollback gates, closure, and required drill evidence. Actual authorized production incident drills and recovery/restore evidence remain outstanding.
- **Next action:** Connect bounded, secret-free telemetry to actual execution paths; define metric windows, label/cardinality limits and alert thresholds. Test injected metrics, export/notification failures and dashboard/runbook integration without logging prompts or payloads.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


### P — Future-Ready Extensibility

#### #129 — Plugin-based provider adapters [PARTIAL]
- **Recorded gap:** `projects/generation/providers/provider_manager.py` now validates provider names/class names, restricts module imports to `projects.generation.providers`, rejects traversal/absolute/backslash/non-Python paths and duplicate module entries, stages all instances before swapping the active registry, and preserves the existing registry on failed reload. The loader also requires each instantiated image provider to expose callable `generate_image` before activation. Six regression tests cover trusted loading, traversal rejection, duplicate/invalid entries, atomic failure, stale-provider removal, and missing-interface rejection; all six pass on Ubuntu and Windows in run [#40391867877](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40391867877). Process isolation, signed plugin packages, and full integration with the provider-store catalog remain open.
- **Next action:** Complete adapter lifecycle integration with the canonical catalog, dependency/version constraints and safe rollback. Keep imports within trusted package boundaries; add malicious-path, invalid-interface, init-failure, unload and isolation tests.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #131 — Validated metadata کے ساتھ capability discovery [PARTIAL]
- **Recorded gap:** Validated capability metadata and discovery exist; live provider metadata trust is incomplete.
- **Next action:** Populate trusted provider/model/version/capability/region/pricing metadata from official sources; reject unknown or contradictory metadata, refresh safely, and test that routing never selects a provider missing required capability/model evidence.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #132 — نئے providers کو محفوظ طریقے سے شامل کرنے کا workflow [PARTIAL]
- **Recorded gap:** Added `ProviderOnboardingWorkflow` to validate candidate IDs/capabilities and subscription policy, require official HTTPS docs for API providers, reject credential values and duplicate providers, perform explicit bounded probe verification, and register candidates disabled/LOCKED only after authenticated `provider.configure` authorization. Missing authorization managers and denied principals fail closed. Tests cover malformed metadata/URLs, secret rejection, strict probe outcomes, disabled-by-default registration, duplicate prevention, authorization denial, and real catalog integration. Evidence: `tests/test_provider_onboarding.py`, run [#40446513581](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40446513581). Authorized activation approval and real provider-specific probe integrations remain open. Evidence: `tests/test_provider_onboarding.py`.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #133 — Self-hosted اور local models کی support، جہاں ممکن ہو [PARTIAL]
- **Recorded gap:** Added `LocalModelProviderAdapter` for local Ollama `/api/generate` text inference; remote/private-network endpoints, URL credentials, query/fragment injection, redirects and malformed/oversized responses are rejected. Installation discovery, model lifecycle/health checks, other local runtimes/modalities, and live server verification remain incomplete. Evidence: `tests/test_local_model_provider_adapter.py`, run [#40389697191](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40389697191), including direct `execute()` provider/request-ID validation.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #134 — Model deprecation اور end-of-life handling [PARTIAL]
- **Recorded gap:** Catalog schema records a validated deprecation reason/date and rejects deprecated providers that are enabled; OpenAI Sora is marked deprecated and disabled. General deprecation discovery, replacement migration, and EOL routing remain open. Evidence: `tests/test_provider_catalogs.py::test_deprecated_provider_requires_reason_date_and_stays_disabled`.
- **Next action:** Inspect the original requirement and existing implementation, identify the missing behavior, implement within existing architecture (no duplicate manager), add focused positive/negative regression tests, run full Windows + Ubuntu CI, and record concrete evidence only.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #137 — Provider configuration اور policy کے لیے admin controls [PARTIAL]
- **Recorded gap:** `ProviderService.set_provider_enabled` and `configure_provider` now require explicit role authorization, fail closed when no authorization manager is configured, and forward paid-usage approval to the connection manager. Viewer-level mutation and authorization-failure regressions pass. Full policy editing, tenant-scoped administration, external identity integration, and management UI remain open. Evidence: `tests/test_provider_service.py::ProviderAdminControlTests`, run [#40197348705](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40197348705).
- **Next action:** Validate candidate config before atomic activation, retain last-known-good state, version migrations, back up before changes and prove restore/rollback on a disposable fixture. Test malformed config, interruption, restart and cross-platform paths.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.

#### #138 — Documentation، diagnostics and maintenance tooling [PARTIAL]
- **Recorded gap:** `provider_diagnostics.py` performs read-only, bounded-size JSON state checks, rejects duplicate object keys/non-finite numbers, detects duplicate paths, and emits secret-free remediation hints. The actual file read is capped as well as the pre-read size check, preventing file-growth races; path-resolution errors fail closed without exposing raw exception details. Regression tests cover both cases. Full maintenance/repair workflows remain open. Evidence: `tests/test_provider_diagnostics.py`, run [#40369663801](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40369663801).
- **Next action:** Document safe operator workflows and exact commands. Diagnostics remain read-only by default, bounded, secret-free and actionable; test corruption/permission/path edges and verify backup/restore or repair procedures.
- **Acceptance:** full canonical scope implemented; focused positive/negative tests pass; full Windows and Ubuntu CI passes; evidence is recorded. If live/production proof is missing, leave PARTIAL.


## Before reporting any percentage

- [ ] Re-fetch and parse the canonical ledger; counts must sum to exactly 138.
- [ ] Count VERIFIED only; report PARTIAL and OPEN separately.
- [ ] Verify the newest commit SHA and latest Windows/Ubuntu CI results.
- [ ] Confirm no secret files/credentials entered the commit.
- [ ] Ensure each changed feature has exact tests and verifiable evidence.
- [ ] Compute actual progress as VERIFIED / 138 × 100; label any broader “implemented” estimate separately.
- [ ] Do not send routine status before the agreed 81% VERIFIED threshold unless the user directly asks.
- [ ] Do not claim Task #25 complete unless all 138 features and release evidence pass.

This handoff does not authorize bypassing approval, security, privacy, spending limits or release gates.