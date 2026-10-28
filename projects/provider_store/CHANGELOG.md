# Changelog — AI_CREATOR_AGENT Provider Store

Changes are recorded here by release intent. This file does not imply a published release.

## Unreleased — 2026-10-28

### Added
- Validated atomic provider-catalog import/export with interprocess locking, backup, post-write validation, and rollback on failed writes.
- Catalog diff/apply CLI with dry-run as the default and explicit approval required for provider removals.
- Timestamped catalog snapshots, safe history listing, and schema-validated restore.
- SPDX 2.3 source-file SBOM containing SHA-256 hashes for packaged files.
- Pinned isolated wheel build metadata and a cross-platform verifier for reproducible wheel output, clean-target installation, module import, and catalog JSON inclusion.
- Focused Ruff, mypy, and coverage gates for provider-store maintenance/release modules.
- Bounded poll-driven health scheduler with injected probes and explicit timeout propagation.
- Outage detector that opens/deduplicates incidents for unhealthy or stale health evidence and resolves them after verified recovery.
- Secret-safe structured JSON event logging integrated into provider connection and manual verification paths.
- Pre-package secret scanning for high-confidence GitHub, OpenAI-style, AWS and Slack token patterns, in addition to private-key blocks.
- Strict `--require-complete` release-report mode and a manual/tag-triggered Advanced Provider Store release gate that blocks artifact upload until all 150 acceptance points pass.

### Fixed
- Preserve original configuration bytes and temporary-file cleanup when config migration replacement fails.
- Preserve rollback history when the previous configuration fails validation.
- Ensure generated SBOM JSON ends with a real newline and remains parseable.
- Include root wheel build metadata in the source release bundle and manifest when present.
- Preserve normal incomplete-ledger report generation while making explicit release-gate mode fail closed.

### Verification
- GitHub Actions run [#40581121875](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40581121875) passed 480 tests on Ubuntu and Windows, focused Ruff lint/format gates, mypy checks for six maintenance/release modules, and 80% combined statement coverage across seven targeted modules.
- Wheel/reproducibility CI [#40581736079](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40581736079) passed on Ubuntu and Windows: two isolated builds match byte-for-byte per OS, the wheel installs into a clean target, module import and catalog data succeed, and source ZIP/SBOM generation passes. Cross-OS binary equivalence and transitive dependency hash locking remain partial.
- Latest hardening CI run [#40704591050](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40704591050) passed 497 tests on Ubuntu and Windows, focused Ruff/mypy/coverage gates (83% focused source coverage), secret-token leak-injection checks, strict-ledger release-gate tests, and source report/package generation. The ledger remains incomplete; no release is published.
- Live provider calls, real account credit balances, and billable generation are not claimed.
