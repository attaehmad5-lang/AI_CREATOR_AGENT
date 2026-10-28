# CI Scope Audit — Project Structure / Folders

Date checked: 2026-10-25
Branch inspected: `feature/project-structure-folders-hardening`
Workflow: `.github/workflows/project-structure-folders.yml`

## Directly verified from GitHub Actions
- Run: https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/40091137663
- Run ID: 40091137663
- Result: 6/6 jobs completed successfully.
- Matrix: Ubuntu and Windows × Python 3.11, 3.12, 3.13.
- Each job compiles the listed `projects/*.py` and `tests/test_*.py` files, then runs 10 named unittest modules.

## Scope gap — important
The workflow currently tests the repository's `projects/` implementation (folder engine/explorer, file ops, search, recycle bin, permissions, quota, versioning). It does **not** import or test the separate PDF reconstruction's `wsengine/` package, `web/` API, its 225-test suite, or its demo. Therefore this green workflow is valid evidence for the listed repository feature tests, not for all 200 PDF requirements.

The workflow is path-filtered. A documentation-only commit does not trigger this workflow. The integration gate document therefore has no new CI run attached to its documentation commit; the earlier run is recorded as separate evidence.

## Required next actions
1. Preserve current green tests and do not overwrite existing `projects/` code with the separate engine.
2. Integrate reviewed `wsengine/`, `web/`, and their tests as a distinct package, with dependency and import isolation.
3. Add those paths to the workflow trigger and compile/test commands, or add a separate workflow for the engine.
4. Keep the security limitations visible: custom SHAKE-256 XOR + HMAC at-rest encryption, no built-in TLS/per-client rate limiting, non-production CSRF posture, EICAR-only malware scanning, and fake cloud adapter.
5. Run CI on the source-changing commit; fix any failures; record the run URL and per-feature evidence before updating status.

## Status
- Existing Project Structure CI matrix: PASS (6/6 jobs).
- PDF Workspace Engine integration CI: NOT RUN / NOT VERIFIED.
- 200 PDF feature completion: NOT VERIFIED.
