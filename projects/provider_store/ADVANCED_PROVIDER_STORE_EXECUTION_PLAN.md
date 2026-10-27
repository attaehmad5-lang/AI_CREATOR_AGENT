# Advanced Provider Store — Execution Plan & Verification Ledger

Scope: Advanced Provider Store only; do not mix unrelated AI_CREATOR_AGENT components.

## Execution rules
- Treat `projects/provider_store/provider_catalog.json` as the runtime source of truth.
- Never claim an external provider is connected, available, free, or has credits without provider-specific verification.
- Do not trigger paid requests, subscriptions, purchases, or real generation calls during offline tests.
- Make focused changes, run targeted tests first, then the provider-store regression suite.
- A feature is complete only when implementation, focused tests, safety review, and documentation agree.

## Workstreams
1. Catalog integrity and legacy synchronization.
2. Advanced-category taxonomy and provider membership.
3. Provider discovery and filtering facades.
4. Credential references and secret non-disclosure.
5. Endpoint allowlists and HTTPS validation.
6. Connection state and probe semantics.
7. OAuth/PKCE lifecycle and token safety.
8. Routing and fallback behavior.
9. Credits, quotas, and budget guardrails.
10. Output validation, retry/circuit-breaker behavior, CI and documentation.

## Verified baseline (2026-10-27)
- Central runtime catalog: version 3.0, 54 providers.
- Advanced category set: 6 categories, 18 providers (3 per category).
- Standalone advanced catalog: 18 entries; IDs and categories match the central advanced records.
- Advanced manager is a compatibility facade over `ProviderStoreManager`; it filters by `ADVANCED_CATEGORIES` and does not independently perform generation or live connectivity checks.
- The provider guide explicitly says the full A–P checklist is not claimed complete.
- External connectivity, live credit balances, and paid generation cannot be certified by offline catalog tests.

## Completion gates
- [ ] Focused regression tests executed successfully in a real Python environment.
- [ ] Existing provider-store regression suite passes.
- [ ] Syntax/static checks pass for changed Python files.
- [ ] Identified defects are fixed with focused regression tests.
- [ ] The original 150-point checklist is mapped to concrete implementation/tests; do not substitute Task #25's separate 138-item ledger without confirming equivalence.
- [ ] PR review confirms no secrets, paid actions, unsafe endpoints, or false live-connection claims.
- [ ] Credentialed provider-auth/credit checks are documented as manual/provider-specific where they cannot be safely automated.

## Execution limitation
The GitHub connector can commit repository files, but this chat runtime cannot clone the repository because outbound network access is unavailable, so it cannot directly execute the Python test suite. Tests must run in GitHub Actions or a local checkout before being marked passing.
