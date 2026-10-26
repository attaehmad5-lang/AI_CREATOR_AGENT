# Provider Store – Core Catalog Verification

**Date:** 2026-10-26

## Local verification

- `python -m compileall -q provider_store tests`: PASS
- `python -W error::ResourceWarning -m pytest -q`: 82 passed, 0 failed
- Statement coverage: 98% (981 statements; 20 uncovered)
- Local package installation from the clean-room extraction: PASS
- Wheel build from the clean-room extraction: PASS (wheel SHA-256: `e0cdea959c073ae02d5a83a82c425e5aa59d75b5192b4e76c6029a206cc099c8`)
- SHA-256 manifest verification and ZIP integrity: PASS (49 ZIP entries; 40 manifest entries)
- Archive SHA-256: `840409d5ef5fb575a615194904a196fb40ef934924e3592d3f352a6bdf915469`

## Defects covered by fixes and regressions

- JSON log redaction preserves valid JSON for escaped exception text.
- SQLite connections close if schema initialization fails.
- Audit log zero/negative limits return an empty list.
- Regression tests cover encrypted catalog access rules, primary/backup corruption, malformed SQLite rows, missing-file fingerprints, cache edge cases, nested secret-path detection, deep-redaction immutability, zero-result search filters, malformed encrypted blobs, missing alias/model errors, no-provider fallback, import-state validation/conflicts, malformed storage layout quarantine, HTTP error handling, model/provider validation failures, logging handler idempotence, and search negative filters.

## Remaining release gates — NOT PASS

The environment cannot reach the Python package index. `black`, `flake8`, and `mypy` are unavailable; `./run_checks.sh` cannot install `mypy>=1.8` or build-isolation dependency `setuptools>=61`.

Still required in a network-enabled environment: `black .`, `black --check .`, `flake8 provider_store tests`, `mypy provider_store`, and `./run_checks.sh` with build isolation. Do not treat release verification as 100% complete until these pass.

## GitHub CI follow-up

- Latest checked commit before this documentation refresh: `5a7ddbfc4875745b50c8e1dbd150cff31270e87c`.
- GitHub Actions run `40296846258` completed successfully on both `ubuntu-latest` and `windows-latest`. Compilation, provider JSON catalog validation, and the repository's configured unit/regression test step passed in both jobs.
- This repository workflow does not demonstrate that the standalone archive's formatting, lint, type-check, or build-isolated packaging gates passed. Those release gates remain **NOT PASS** until run directly against the standalone package.
- No PR has been merged.
