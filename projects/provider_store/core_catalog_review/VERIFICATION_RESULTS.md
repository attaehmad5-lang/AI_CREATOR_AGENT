# Provider Store – Core Catalog Verification

**Date:** 2026-10-26

## Local verification

- `python -m compileall -q provider_store tests`: PASS
- `python -W error::ResourceWarning -m pytest -q`: 82 passed, 0 failed
- Statement coverage: 98% (981 statements; 20 uncovered)
- Local package installation from the clean-room extraction: PASS
- Wheel build from the clean-room extraction: PASS
- SHA-256 manifest verification and ZIP integrity: PASS (29 ZIP entries; 28 manifest entries)
- Archive SHA-256: `e5f55e92f8e66156f4d6cf59a358bedbce31443068b809a18e6da5f23af6477a`

## Defects covered by fixes and regressions

- JSON log redaction preserves valid JSON for escaped exception text.
- SQLite connections close if schema initialization fails.
- Audit log zero/negative limits return an empty list.
- Regression tests cover encrypted catalog access rules, primary/backup corruption, malformed SQLite rows, missing-file fingerprints, cache edge cases, nested secret-path detection, deep-redaction immutability, zero-result search filters, malformed encrypted blobs, missing alias/model errors, no-provider fallback, import-state validation/conflicts, malformed storage layout quarantine, HTTP error handling, model/provider validation failures, logging handler idempotence, and search negative filters.

## Remaining release gates — NOT PASS

The environment cannot reach the Python package index. `black`, `flake8`, and `mypy` are unavailable; `./run_checks.sh` cannot install `mypy>=1.8` or build-isolation dependency `setuptools>=61`.

Still required in a network-enabled environment: `black .`, `black --check .`, `flake8 provider_store tests`, `mypy provider_store`, and `./run_checks.sh` with build isolation. Do not treat release verification as 100% complete until these pass.

## GitHub CI follow-up

- GitHub Actions run `40296846258` completed successfully on both `ubuntu-latest` and `windows-latest`. Compilation, provider JSON catalog validation, and the repository's configured unit/regression test step passed in both jobs.
- This repository workflow does not demonstrate that the standalone archive's formatting, lint, type-check, or build-isolated packaging gates passed. Those release gates remain **NOT PASS** until run directly against the standalone package.
- No PR has been merged.


## Repeat verification — 2026-10-26 (latest)

- Re-ran compilation and the full test suite from the clean-room extraction: **82 passed, 0 failed**; coverage remains **98%**.
- Re-ran local package installation and wheel build with `--no-build-isolation --no-deps`: both passed. Wheel SHA-256: `0c3a74ecc46b600627a3448ad91a2501ee659ac603ab3cbf9ff469b54af9f634`.
- Retried installation of Black, Flake8, and Mypy. The package index returned no available versions, so this environment still cannot run those tools. This is an environment/network blocker, not a passing check.
- The configured Ubuntu and Windows workflow jobs were inspected individually; both completed successfully for compilation, provider JSON validation, and configured unit/regression tests. They do not include the blocked standalone lint/type/build-isolation gates.
