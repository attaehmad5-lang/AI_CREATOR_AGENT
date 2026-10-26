# Provider Store – Core Catalog Verification

**Date:** 2026-10-26  
**Scope:** User-provided Provider Store – Core Catalog package, isolated Linux workspace.

## Latest verification

- `python -m compileall -q provider_store tests`: **PASS**
- `python -W error::ResourceWarning -m pytest -q`: **69 passed, 0 failed**
- Statement coverage: **95%** (981 statements; 53 uncovered)
- Local package installation from extracted archive with `python -m pip install --no-build-isolation --no-deps .`: **PASS**
- Wheel build with `python -m pip wheel --no-build-isolation --no-deps .`: **PASS**
- SHA-256 manifest verification and ZIP integrity check: **PASS**.
- Cleaned release archive: **25 ZIP entries**, no `build/` or cache artifacts; all 24 manifest entries verified. Archive SHA-256: `3fefc579aeb10483c65b525d6261dc76d81ad3d27232d8503862cd333b14f7f0`.
- Documentation corrected so the setup guide no longer contradicts the executed test evidence; blocked developer-tool gates remain explicitly NOT PASS.

## Defects fixed

1. `JsonFormatter.format()` redacts payload values before JSON serialization, avoiding corrupted escaped exception text.
2. `SqliteStorage._connect()` closes a connection if schema initialization fails.
3. `AuditLog.entries(limit=0)` and negative limits now return an empty list.
4. Regression tests cover escaped exception JSON, non-positive audit limits, and SQLite connection closure when schema initialization fails.
5. Additional regression tests cover encrypted catalog access without a cipher, simultaneous primary/backup corruption, malformed SQLite rows, and missing-file fingerprints. The test fixture explicitly closes its SQLite connection.

## Remaining release gates — NOT PASS

The execution environment cannot resolve/reach the Python package index. `black`, `flake8`, and `mypy` are unavailable; `./run_checks.sh` cannot install `mypy>=1.8` or the build-isolation dependency `setuptools>=61`.

Still required in network-enabled CI or Termux:
- `black .` and `black --check .`
- `flake8 provider_store tests`
- `mypy provider_store`
- `./run_checks.sh` clean-room build-isolated install

This is not a claim of 100% release verification until all release gates pass. No PR has been merged.
