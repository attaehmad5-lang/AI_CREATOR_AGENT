# Provider Store Production Readiness Runbook

This runbook distinguishes repository-verified behavior from deployment-owned work. A green CI run is necessary, but it does not prove that real provider credentials, billing policies, or production infrastructure are configured.

## 1. Required host wiring

The application host must construct and inject:

- The canonical `projects.provider_store.provider_service.ProviderService` (with its reviewed connection, routing, cost, quota, authorization, and output-validation dependencies).
- A trusted provider executor callable. Never load executable Python factories from provider catalog data.
- Explicit principal/project authorization and user-consent context.
- A provider-specific cost estimate and approved project budget. Unknown price must fail closed; no automatic purchase or paid call.
- A request ID that remains stable across retries of the same logical request.
- An output validator and, where enabled, an artifact scanner.

Pass the canonical service and executor into `GenerationAdapter(provider_store_service=..., provider_store_adapter=...)` and `ExecutionGateway`. Keep the legacy path only for compatibility; production configuration must explicitly select the canonical path. Do not use placeholder executors in production.

## 2. OAuth refresh-token wiring

Use `SecureHTTPTransport` with a strict exact-host allowlist and bounded timeouts, then pass it to `SecureOAuthTokenRequester`, and inject that requester into `OAuth2RefreshTokenManager`.

- Store client ID, client secret, and refresh token in the deployment secret manager/environment, never in source, catalog JSON, logs, or checked-in files.
- Verify each provider's documented token endpoint and authentication method independently.
- If refresh-token rotation is required, configure a durable secret-manager sink and verify that the new refresh token is persisted before the old one is considered replaceable.
- Keep browser authorization-code/PKCE consent as a separate provider-specific flow; this refresh-token manager does not implement it.
- Use a non-billable, provider-documented endpoint for live authorization probes. Never infer account credit or generation access from a generic HTTP 200.

## 3. Malware and media validation

Before enabling the ClamAV adapter in a deployment:

1. Install a supported ClamAV engine and update signatures through the organization's approved process.
2. Run a harmless EICAR test file in an isolated test environment and confirm it is rejected; never use a real malicious sample.
3. Confirm timeout, unavailable-engine, oversized-file, symlink, and scanner-error cases fail closed.
4. Validate output signatures, size, and checksum, then add actual media decoding/codec validation appropriate to each enabled format. Signature checks alone are not full media validation.

## 4. Security and reliability acceptance

Before production release, attach evidence for all of the following:

- [ ] Controlled HTTPS integration tests covering certificate validation, redirect limits, HTTPS downgrade rejection, DNS rebinding, non-public IP rejection, credential stripping across origins, request/response size bounds, and timeout behavior.
- [ ] Provider-specific OAuth or API-key tests against documented, non-billable endpoints using explicitly authorized test accounts.
- [ ] Cost/quota reservation and reconciliation tests for success, provider failure after request start, timeout with ambiguous outcome, cancellation, retry, fallback, and process restart.
- [ ] Cross-process concurrency tests for reservations and state changes. For multi-worker or multi-host deployments, use a transactional database or distributed coordination rather than assuming local JSON lock files coordinate across hosts.
- [ ] Media decoding and semantic validation, fuzz/mutation tests, load tests, and measured latency/error/throughput budgets.
- [ ] Secret redaction review, dependency/security scan, backup/restore drill, recovery drill, alerting, dashboards, and an operator runbook.
- [ ] Production configuration reviewed by the project owner; no placeholder secrets, permissive host allowlists, disabled TLS verification, or implicit paid approval.

## 5. CI verification commands

From the repository root:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
python -m unittest tests.test_oauth2_refresh_manager -v
python -m unittest tests.test_artifact_scanner -v
```

Use the repository's Provider Store workflow for the full Python 3.12/3.13 Ubuntu/Windows matrix and the core coverage gate. Record the run URL and exact tested commit in the release evidence.

## Release decision

Keep the PR in draft until the repository CI passes and the deployment-owned acceptance items above are either completed with evidence or explicitly waived by the project owner. No live or paid provider test is authorized merely by this runbook.
