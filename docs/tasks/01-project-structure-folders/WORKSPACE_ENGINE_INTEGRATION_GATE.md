# Workspace Engine Integration Gate — Feature #1 (200 features)

## Non-negotiable completion rule
A feature is COMPLETE only when all of these are true:
1. Its numbered requirement (1–200) is mapped to an actual repository code path.
2. Focused positive, negative, boundary, and regression tests exist and pass.
3. Security-sensitive behavior has an explicit threat/limitation review.
4. The full repository test suite and CI pass on the same commit.
5. Evidence is recorded below with commit/run links. PDF status labels alone are not evidence.

## Baseline facts
- Source PDF has exactly 200 numbered rows (rows 201–210 are supplemental, not part of this count).
- PDF-reported status: 153 Full, 47 Partial. These are source-document claims, not repository completion.
- Reconstructed package passed a fresh local verification on 2026-10-25: compileall PASS; 225 unittest methods PASS; demo PASS (upload/search/version compare/script organization/backup verification/health/integrity); ZIP integrity PASS.
- These results apply to the repaired reconstruction archive, not to the AI_CREATOR_AGENT repository or Pydroid 3.
- Audit: PROJECT_STRUCTURE_FOLDERS_200_FEATURE_AUDIT.md
- Source manifest: WORKSPACE_ENGINE_PDF_SOURCE_MANIFEST.md

## Integration sequence
- [ ] Import reviewed source files into a dedicated feature branch, preserving original provenance and the 1–200 numbering.
- [ ] Resolve package/import conflicts with existing AI_CREATOR_AGENT modules.
- [ ] Add dependency and runtime compatibility checks for the supported Python versions and Pydroid 3.
- [ ] Replace or explicitly block production use of custom SHAKE-256 XOR + HMAC encryption; use an audited authenticated-encryption design.
- [ ] Harden web/API deployment: TLS termination requirements, CSRF, per-client rate limits, authentication/session controls, and safe defaults.
- [ ] Replace EICAR-only malware detection with a real scanner adapter or mark the feature PARTIAL with a documented adapter contract.
- [ ] Exercise cloud adapter against a real supported provider or keep it PARTIAL; fake-client tests are insufficient.
- [ ] Validate Android/iOS items as native-app integration only if actual native clients exist; server-side payload adapters do not satisfy native integration.
- [ ] Run compileall, full unittest suite, security checks, demo, package/ZIP integrity, then GitHub Actions.
- [ ] Fix every failure and rerun affected tests plus full suite.
- [ ] Update the 200-row ledger with per-feature code/test/CI evidence.
- [ ] Request review through PR; do not merge without owner approval.

## Fresh security triage (static inspection; not a penetration test)
- `wsengine/security.py` includes custom at-rest encryption described in the source audit as SHAKE-256 XOR + HMAC; do not treat it as production-ready.
- Password verification uses PBKDF2-HMAC-SHA256 with 20,000 iterations in one auth path and 5,000 iterations for share-link passwords; these parameters need a current security review and upgrade/migration tests before production.
- Malware scanner defaults to an EICAR test signature only; this is not a real antivirus integration.
- `web/server.py` uses Python's `ThreadingHTTPServer`; TLS must be provided and correctly enforced by a trusted reverse proxy or replaced by a hardened deployment layer. Do not expose the development server directly to the public internet.
- Static inspection found a rate-limit response in an integration component, but this alone does not prove per-client HTTP rate limiting.

## Evidence ledger
| Gate | Status | Evidence |
|---|---|---|
| Source PDF feature count and numbering | PASS | Audit documents 200 rows; supplemental 201–210 excluded |
| Reconstructed package syntax | PASS (local reconstruction only) | compileall completed without errors on 2026-10-25 |
| Reconstructed package unit tests | PASS (local reconstruction only) | 225 unittest methods passed on 2026-10-25 |
| Reconstructed demo and ZIP integrity | PASS (local reconstruction only) | Demo ended OK; unzip reported no errors |
| Integrated into AI_CREATOR_AGENT branch | NOT YET VERIFIED | Source modules not yet shown in repository tree |
| GitHub CI for integrated engine | NOT YET VERIFIED | No workflow runs/status checks returned for integration-gate documentation commit |
| Production security review | NOT PASS | Encryption, password KDF parameters, TLS/deployment, and malware scanning need remediation/review |
| All 200 features repository-verified | NOT PASS | Requires per-feature code/test/CI evidence |

## Status discipline
Do not report a percentage as verified unless it is calculated from feature rows whose evidence gates pass. Never convert “test method passed” into “feature complete” without confirming the test exercises the actual requirement and production code path.
