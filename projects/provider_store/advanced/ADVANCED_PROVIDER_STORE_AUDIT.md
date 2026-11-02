# Advanced Provider Store — Feature Verification

This document tracks the Advanced Provider Store as a distinct feature. It is not the overall Task #25 completion record.

## Scope

- Read advanced provider definitions from the validated central provider catalog.
- Filter strictly to the declared advanced categories: `script_story`, `advanced_image_editing`, `advanced_upscaling`, `advanced_transcription`, `advanced_audio`, and `advanced_video_editing`.
- Provide provider lookup, category filtering, and a compatibility catalog view.
- Report catalog validity, enabled/disabled counts, and live verified connection counts separately.
- Never treat catalog labels or a successful generic probe as proof that a provider can generate a specific modality.
- Keep the central ConnectionManager as the source of truth; do not create a second independent registry.

## Current implementation evidence

- `advanced/advanced_provider_manager.py` implements the advanced-only facade and evidence-based status fields.
- `provider_catalog.json` is the central catalog; `advanced/advanced_provider_catalog.json` is a compatibility/reference view, not an independent runtime registry.
- `advanced/advanced_categories.json` separates capabilities represented by current catalog entries from planned capabilities.
- `tests/test_advanced_provider_store.py` covers advanced-only filtering, lookup, constructor validation, status accounting, verified-state behavior, negative connection paths, exception-message sanitization, exact catalog identity/auth synchronization, supported-versus-planned capability declarations, and deep detachment of nested values returned by list/get/compatibility catalog views.
- `tests/test_provider_store_compat.py` covers shared-catalog compatibility with the unified registry, router, and legacy facades.

## Completion gates

| Gate | Required evidence | State |
|---|---|---|
| Catalog and category filtering | Unit tests, including core providers excluded from advanced views | PASS |
| Catalog integrity | Exact advanced-provider identity/auth metadata synchronization with the central catalog | PASS — PR #92 |
| Capability truthfulness | Supported capabilities match current provider declarations; future capabilities remain explicitly planned | PASS — PR #92 |
| Status accuracy | Catalog validity, enabled/disabled, and verified-connected counts tested separately | PASS |
| Negative-path safety | Disabled providers, invalid probes/timeouts, failed probes, exceptions, and unknown/core IDs cannot create false verified connections | PASS — PR #91 |
| Compatibility | Existing provider-store compatibility tests remain green | PASS — PR #91/#92 CI |
| Cross-platform CI | Compile, catalog validation, and unit/regression tests on Ubuntu and Windows | PASS — workflow runs [41221773168](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/41221773168) and [41222470262](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/41222470262), both Ubuntu and Windows jobs successful |
| Detached public views | Mutating nested values returned by list/get/compatibility catalog views cannot mutate the central catalog | PASS — [PR #94](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/94), Ubuntu and Windows CI passed |
| Release | Reviewed changes merged to main | PASS — [PR #91](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/91), [PR #92](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/92), [PR #93](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/93), and [PR #94](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/94) merged |
| Operational verification | Authorized provider-specific connection and modality tests | DEPLOYMENT GATE — requires credentials configured securely in the runtime environment and provider-specific non-billable verification endpoints where available |

## Security and correctness notes

- Never store API tokens in this feature's catalog, test fixtures, commits, or CI logs.
- A provider being enabled is not the same as being connected.
- A verified connection is not the same as verified modality generation.
- Catalog readiness means the catalog is valid and accessible; it does not mean every provider is enabled, subscribed, authenticated, or live.
- CI uses deterministic local fixtures; it does not claim external provider APIs were contacted.
- The Advanced Provider Store is verified at the catalog, state-management, filtering, compatibility, and regression-test levels by the merged changes and the Ubuntu/Windows CI run above.
- Live external connection and modality verification remains a deployment gate. It must use an authorized, securely configured provider; it is not safe to fabricate a pass or run potentially billable generation calls without authorization.


## Additional security verification (2026-10-31)

- PR #96: provider registration now validates the complete candidate catalog before persistence; invalid metadata is rejected without changing the catalog. Ubuntu and Windows CI passed (workflow run 41223117609).
- PR #97: credential-reference exemptions accept only exact canonical lowercase underscore keys; case and hyphen aliases are rejected. Four registration regression cases confirm rejection without persistence. Ubuntu and Windows CI passed (workflow run 41223513751).
- Both PRs are merged to `main`. These checks do not establish live provider authentication or modality generation; operational verification remains a deployment gate.

- PR #99: the final connectivity-success transition now rechecks provider enablement under the catalog transaction lock. If a provider is disabled during an in-flight probe, a late successful result is persisted as `DISABLED` and unverified, not `CONNECTED`. The deterministic concurrency regression passed on Ubuntu and Windows (workflow run 41224126274); PR #99 is merged to `main`.
- PR #100: audit documentation was refreshed after the successful connectivity-race fix; Ubuntu and Windows CI passed (workflow run 41224245402).
- PR #103: a failed in-flight connectivity probe can no longer overwrite a concurrent administrative disable with `ERROR`; the failure path now preserves `DISABLED` and returns an unverified disabled result. The deterministic concurrency regression passed on Ubuntu and Windows (workflow run 41224697520); PR #103 is merged to `main`.
- PR #104: audit evidence refreshed for PR #100 and the failed-probe race fix; Ubuntu and Windows CI passed (workflow run 41224912867).
- PR #105: manual/account-session connectivity probes now preserve an administrative `DISABLED` state when an in-flight probe returns failure or raises an exception. Deterministic concurrency tests cover both outcomes; Ubuntu and Windows CI passed (workflow run 41225086539). PR #105 is merged to `main`.
- PR #107: disabling a provider now immediately revokes a previously verified `CONNECTED` or in-flight `CONNECTING` state as `DISABLED`/unverified, while preserving `UNKNOWN` for providers with no connection attempt. Regression coverage and Ubuntu/Windows CI passed (workflow run 41225269136); PR #107 is merged to `main`.

- PR #108: audit evidence refreshed for the provider-disable state revocation fix; Ubuntu and Windows CI passed (workflow run 41225376033).
- PR #109: provider state reads now fail closed against the authoritative catalog when a disabled provider has stale persisted connection data. The public state view reports `DISABLED`/unverified for stale connected or connecting records, and summary counts cannot report them as connected. Regression tests cover stale state and a simulated state-file write failure during disable. The first CI run exposed a compatibility regression for never-connected disabled providers; the implementation was corrected to preserve their `UNKNOWN` state. Corrected commit `bbcdc41f8df5345e139cd5a369060cdaa789f873` passed Ubuntu and Windows CI (workflow run 41232618100); PR #109 is merged to `main`.

- PR #111: public `set_state()` updates now serialize provider eligibility checks and state persistence with catalog disable operations. Attempts to set `READY`, `ERROR`, or `CONNECTING` on an explicitly disabled provider preserve `DISABLED`/unverified. Regression coverage and Ubuntu/Windows CI passed (workflow run 41234343829); PR #111 is merged to `main`.
- PR #112: `configure_provider()` now rejects malformed or credential-bearing existing persisted configuration before merging a new setting. This prevents a safe update from returning an unsafe pre-existing credential field in its merge response. Regression coverage and Ubuntu/Windows CI passed (workflow run 41235361947); PR #112 is merged to `main`.

- PR #113: audit evidence refreshed for the disabled-state setter and configuration-secret protections in PRs #111–#112. Ubuntu and Windows CI passed (workflow run 41235495613); PR #113 is merged to `main`.
- PR #114: runtime provider configuration now accepts only the explicit fields `model`, `endpoint`, `probe_endpoint`, and `authentication_required`. Unknown keys—including catalog-only environment-reference keys—are rejected on both read and update paths. Regression tests cover secret-like values and nested unknown fields; Ubuntu and Windows CI passed (workflow run 41235648255); PR #114 is merged to `main`.
- PR #115: existing persisted configuration fields are validated before any partial update is merged. Malformed authentication policy, model values, or endpoints cannot be carried forward while a new field is reported as saved. Regression coverage and Ubuntu/Windows CI passed (workflow run 41235814516); PR #115 is merged to `main`.


## Additional regression hardening (2026-11-02)

- Advanced catalog compatibility views now derive their status, catalog_valid, and ready fields from the same live schema validation used by status() instead of always claiming READY. This prevents a stale or invalid catalog from being represented as ready in the compatibility facade.
- by_category() now safely rejects non-string and unhashable inputs instead of raising a runtime TypeError.
- Added regression tests for both cases. **PASS / released:** PR [#152](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/pull/152) merged as `e5811956fa10847a50b611075290a947452f9e94`; GitHub Actions run [#41380517071](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/41380517071) passed on Ubuntu and Windows, with 518 tests passing on each platform, zero failures, compile checks and provider JSON catalog validation passing.
