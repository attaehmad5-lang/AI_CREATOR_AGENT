# Provider Store – Core Catalog Verification

**Date:** 2026-10-26

## Local verification

- `python -m compileall -q provider_store tests`: PASS
- `python -W error::ResourceWarning -m pytest -q`: 69 passed, 0 failed
- Statement coverage: 95% (981 statements; 53 uncovered)
- Local package install and wheel build: PASS
- SHA-256 manifest verification and ZIP integrity: PASS (25 ZIP entries; 24 manifest entries)
- Archive SHA-256: `3711a49a73e53067f821df32b0a524f85f41d2bb7e7bac711d2893975e1384ed`

## Defects covered by fixes and regressions

- JSON log redaction preserves valid JSON for escaped exception text.
- SQLite connections close if schema initialization fails.
- Audit log zero/negative limits return an empty list.
- Regression tests cover missing cipher for encrypted catalogs, simultaneous primary/backup corruption, malformed SQLite rows, and missing-file fingerprints.

## Remaining release gates — NOT PASS

The environment cannot reach the Python package index. `black`, `flake8`, and `mypy` are unavailable; `./run_checks.sh` cannot install `mypy>=1.8` or build-isolation dependency `setuptools>=61`.

Still required in a network-enabled environment: `black .`, `black --check .`, `flake8 provider_store tests`, `mypy provider_store`, and `./run_checks.sh` with build isolation. Do not treat release verification as 100% complete until these pass.

## GitHub CI follow-up

GitHub Actions run 40259177921, associated with commit `7f5ca779c7be0a8acc8b8c3cb667c948fcfba8ce`, completed successfully. Both Ubuntu and Windows jobs passed compilation, provider JSON catalog validation, and the repository's configured unit/regression test step. This is not evidence that the standalone archive's formatting, lint, type-check, or build-isolated packaging gates passed. No PR has been merged.
