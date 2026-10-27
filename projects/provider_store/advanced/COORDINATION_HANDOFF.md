# Advanced Provider Store — Shared AI Work Handoff

Scope: **Advanced Provider Store only. This is not Task #25 / Provider Connection Manager.**

## Coordination rules for any AI or human contributor
1. Start by reading `PRODUCTION_READINESS.md`, this handoff, the current PR #82 diff, and current CI results.
2. Work only on the `task-advanced-provider-store-review` branch or a separate clearly named branch/PR based on its latest commit. Do not overwrite another contributor's changes.
3. Before editing a file, check its latest GitHub blob/commit. Use small, focused commits. If the branch head changes while editing, refetch and rebase/reapply rather than overwriting newer content.
4. Add a regression test for each behavior change. Never mark a feature complete based on implementation text alone.
5. Do not run paid provider requests or use production credentials. Use mocked/local fixtures unless the project owner explicitly authorizes a non-billable live test.
6. Keep Task #25 progress, counts, PRs, and test runs separate from this task.
7. Do not merge or claim production-ready while required CI/security/deployment gates remain open.

## Confirmed current implementation / repository evidence
- Core Advanced Provider Store modules cover catalog/versioning, atomic local storage and recovery, audit redaction/hash chain, endpoint policy, bounded HTTPS transport, environment-backed credential references, health leases, allowlisted runtime loading, media signature/size/checksum validation, resilience/routing/accounting, and persistent job lifecycle.
- Integration adapters exist in `agents/generation/generation_adapter.py` and `agents/execution/execution_gateway.py`, with tests.
- Core tests include storage recovery, catalog/rollback, security guards, redaction/audit, accounting, ambiguous outcomes, circuit breaker, CLI, media validation, runtime loading/unloading, health leases, credentials, job recovery/cancellation, secure transport, and routing.
- A new regression test in `tests/test_generation_adapter_provider_store.py` verifies untrusted adapter options cannot override trusted routed provider identity, request ID, prompt, or output path.
- A further regression test verifies non-mapping provider-service results are surfaced as ambiguous outcomes requiring reconciliation.
- These latest tests must be confirmed by a CI run for the latest branch head before being counted as passing.

## Remaining production gates — take these in parallel only across non-overlapping files
A. **Host integration:** demonstrate real application composition injects canonical `ProviderService`, trusted executor, principal/project authorization, user consent, validated cost/budget, stable request ID, output validator, and optional scanner. Remove accidental default-path assumptions only with backward-compatibility tests.
B. **HTTPS adversarial tests:** add controlled local TLS-server tests for certificate rejection, redirects and HTTPS downgrade, DNS rebinding/pinning, private-address rejection, cross-origin credential stripping, size bounds, and timeouts. Keep fixtures deterministic; no external provider call.
C. **OAuth:** add provider-agnostic authorization-code/PKCE state/nonce/redirect validation as a separate module only if product requirements call for it; keep refresh-token manager distinct. Test token rotation persistence and invalid/expired responses using fake transport.
D. **Scanner/media:** test ClamAV adapter behavior with a fake runner and an optional locally installed-engine integration test that skips cleanly when unavailable. Use only harmless EICAR test fixture in an isolated environment; never a real malware sample. Add actual decoder/codec validation only for supported media formats.
E. **Accounting and concurrency:** expand failure-injection tests for timeout after request start, cancellation, retries/fallback, restart reconciliation, duplicate requests, and cross-process reservation races. Local JSON locking is not multi-host coordination.
F. **Release evidence:** run full Python 3.12/3.13 Linux/Windows matrix, coverage gate, compileall, package/wheel checks, security/dependency scan, secret scan, backup/restore drill, and document exact tested commit and artifacts. Keep deployment-only items explicitly open until performed in a real controlled environment.

## Latest known workflow snapshot (refresh before relying on it)
Workflow run `40521209734` previously completed the full-repository Python 3.13 Ubuntu regression and Provider Store Python 3.12/3.13 Ubuntu jobs successfully. At the last observation, Windows matrix jobs were still running. This snapshot predates the latest test commits and is not proof that the latest head passes.

## Status discipline
Use statuses: `PASS` (implementation + relevant test + recorded result), `PARTIAL` (implemented but evidence/integration incomplete), `FAIL` (reproducible failure), `BLOCKED` (requires owner/deployment access), `NOT STARTED`. Never convert `UNVERIFIED` to `PASS` without evidence.
