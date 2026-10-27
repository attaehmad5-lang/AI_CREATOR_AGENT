# Changelog — AI_CREATOR_AGENT Provider Store

Changes are recorded here by release intent. This file does not imply a published release.

## Unreleased — 2026-10-27

### Added
- Validated atomic provider-catalog import/export with interprocess locking, backup, post-write validation, and rollback on failed writes.
- Catalog diff/apply CLI with dry-run as the default and explicit approval required for provider removals.
- Timestamped catalog snapshots, safe history listing, and schema-validated restore.
- SPDX 2.3 source-file SBOM containing SHA-256 hashes for packaged files.
- Pinned isolated wheel build metadata and a cross-platform verifier for reproducible wheel output, clean-target installation, module import, and catalog JSON inclusion.
- Focused Ruff, mypy, and coverage gates for provider-store maintenance/release modules.

### Fixed
- Preserve original configuration bytes and temporary-file cleanup when config migration replacement fails.
- Preserve rollback history when the previous configuration fails validation.
- Ensure generated SBOM JSON ends with a real newline and remains parseable.
- Include root wheel build metadata in the source release bundle and manifest when present.

### Verification
- GitHub Actions run [#40581121875](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40581121875) passed 480 tests on Ubuntu and Windows, focused Ruff lint/format gates, mypy checks for six maintenance/release modules, and 80% combined statement coverage across seven targeted modules.
- A newer wheel-build/reproducibility gate is in progress; its result must be recorded before calling wheel installation or reproducible builds verified.
- Live provider calls, real account credit balances, and billable generation are not claimed.
