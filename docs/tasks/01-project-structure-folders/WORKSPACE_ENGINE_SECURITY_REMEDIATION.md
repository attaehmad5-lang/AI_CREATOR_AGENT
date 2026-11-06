# Workspace Engine Security Remediation Plan

Status: remediation requirements, not a claim that fixes have shipped.

## Release-blocking findings from the source-PDF audit
1. At-rest encryption: current custom SHAKE-256 XOR + HMAC construction is not an audited AEAD scheme. Do not use for production data. Replace with a maintained authenticated-encryption implementation (e.g. AES-GCM or ChaCha20-Poly1305 from a vetted library), with unique nonces, key separation, rotation/versioning, tamper tests, and documented key management. If the dependency cannot be installed in a target runtime, fail closed or explicitly disable encryption-dependent operations; never silently fall back to custom crypto.
2. HTTP transport: server has no built-in TLS. Production deployment must terminate TLS at a documented trusted reverse proxy or use a maintained TLS server. Reject insecure production configuration and test proxy/header trust boundaries.
3. CSRF/session protection: define cookie flags, origin checks, CSRF token validation for state-changing browser requests, and tests for missing/invalid tokens and cross-origin requests.
4. Rate limiting: current limits are in-process. Add per-client limits at a trusted gateway or distributed store for multi-worker deployments; never trust arbitrary forwarded client IP headers.
5. Malware scanning: EICAR-only signature detection is a test fixture, not malware protection. Introduce a scanner interface with timeout, failure policy, quarantine behavior, and explicit “scanner unavailable” status. Do not report a clean result when scanning was skipped.
6. Cloud adapter: fake in-memory tests do not prove real provider compatibility. Keep feature status PARTIAL until an actual provider integration test is run with disposable test resources and no secrets in logs.
7. Mobile adapters: server-side payload adapters are not native Android/iOS integrations. Keep native-app feature rows PARTIAL until client implementation and integration tests exist.
8. Observability and concurrency: process-local metrics, locks, and quotas do not coordinate across workers. Document single-process limits or provide shared backends and multi-process tests.

## Required test categories for each security change
- Positive and negative cases; malformed input; boundary conditions.
- Tamper detection and nonce/key lifecycle tests for encryption.
- Authorization bypass and cross-project isolation tests.
- CSRF, origin, cookie, proxy-header and rate-limit tests.
- Failure injection: scanner/provider unavailable, timeouts, disk full, interrupted writes.
- Regression suite and full CI on the same commit.

## Release gate
No production-ready label until each release-blocking item is fixed or explicitly disabled by a fail-closed configuration, tests pass, and a reviewer signs off. Preserve PDF feature numbering 1–200 in the audit ledger.
