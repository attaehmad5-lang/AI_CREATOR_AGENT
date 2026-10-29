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
- `provider_catalog.json` is the central catalog; the advanced catalog remains a compatibility/reference view.
- `tests/test_provider_store_compat.py` covers shared-catalog compatibility.
- `tests/test_advanced_provider_store.py` adds isolated regression tests for category filtering, status accounting, constructor validation, and verified connection state.

## Completion gates

| Gate | Required evidence | State |
|---|---|---|
| Catalog and category filtering | Unit tests, including core providers excluded from advanced views | PASS — covered by regression suite |
| Status accuracy | Catalog-valid, enabled, disabled, and verified-connected counts tested separately | PASS — covered by regression suite |
| Compatibility | Existing provider-store compatibility tests remain green | PASS — full CI suite |
| Full repository regression | Complete Python test suite on supported CI platforms | PASS — 426 tests passed on Ubuntu and Windows in workflow run 40885872101 |
| Operational verification | Authorized provider-specific connection and modality tests | BLOCKED — requires authorized provider credentials/endpoints; no secrets are stored in the repository |
| Release | Review CI results and merge after code/test gates pass | MERGED — PR #86; code/test gates pass, while external provider verification remains outstanding |

## Security and correctness notes

- Never store API tokens in this feature's catalog, test fixtures, commits, or CI logs.
- A provider being enabled is not the same as being connected.
- A verified connection is not the same as verified modality generation.
- This feature must be tracked independently from the Task #25 A–P checklist.
- CI uses deterministic local fixtures; it does not claim external provider APIs were contacted.
