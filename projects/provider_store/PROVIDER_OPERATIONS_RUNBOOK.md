# Provider Operations Runbook

**Status:** operational draft. Validate these procedures in the target deployment before treating them as production-approved.

## 1. Incident priorities

1. **Protect users and spending first.** Disable the affected provider or generation feature flag if requests may be misrouted, charges are unexpected, or outputs are unsafe.
2. **Preserve evidence.** Keep request IDs, provider IDs, safe status/error codes, timestamps, run IDs, and sanitized metrics. Do not copy prompts, generated content, access tokens, API keys, authorization headers, or raw provider response bodies into incident notes.
3. **Do not retry an ambiguous paid request.** A timeout or client-side exception does not prove the provider did not accept or charge the request. Retain the reservation until the charge outcome is verified.
4. **Restore only after verification.** A green local unit-test run is not proof that an external provider account, endpoint, balance, or generation modality is healthy.

## 2. Triage a provider outage

1. Identify the affected provider, modality, project, and request IDs from safe service metadata.
2. Check the provider's configured enablement, endpoint host allowlist, credential reference/expiry state, and latest health history.
3. Run a provider-specific connectivity probe only with authorized credentials and the documented endpoint. Never print the credential or response body.
4. Distinguish transport reachability from authenticated access, generation capability, and account balance; these are separate checks.
5. If the probe fails, mark the provider unhealthy/disabled according to the approved deployment procedure. Confirm routing excludes it and that fallback policy does not bypass project, region, cost, privacy, or capability constraints.
6. Record a safe incident code and timestamp. Re-admit the provider only after the configured recovery-success threshold and a modality-specific smoke test pass.

## 3. Unexpected charge, budget, or credit exhaustion

1. Stop new paid attempts for the affected project/provider and preserve the request ID.
2. Inspect the cost reservation, finalization/reconciliation status, budget alert events, and provider's documented usage record.
3. If the request outcome is unknown, **do not release the reservation** until the provider confirms whether a charge occurred.
4. Reconcile actual cost against the estimate using the original idempotency key. Do not create a second request ID merely to bypass a duplicate or budget rejection.
5. If free credits are exhausted or a billing/quota error is reported, do not retry as a transient network failure. Require a fresh verified balance or an explicit authorized budget change.
6. Resume only after the project budget, paid approval, credit evidence, and routing policy have been checked. Never auto-purchase a subscription or credits.

## 4. Invalid or unsafe output

1. Quarantine the output reference and do not publish or forward it.
2. Preserve only the safe validation status, modality, size, content hash, and request ID.
3. Check extension/signature/structure validation and configured size/duration bounds. For audio/video, distinguish container-structure validation from full codec decoding and semantic quality checks.
4. Do not mark the generation successful if validation or configured quality policy failed.
5. Retry only when the execution and billing state prove a retry is safe; otherwise keep the reservation and escalate.

## 5. Queue, worker, and stuck-job recovery

1. Check queue depth, priority, project fairness, running leases, attempt count, and safe last-error code.
2. Confirm the worker still owns the job before completing or cancelling it.
3. Let expired leases follow the configured bounded retry policy. Do not manually duplicate a job with a new ID to get around idempotency.
4. For a cancellation request, verify whether provider execution already started; cancellation may not reverse an external charge.
5. If the queue is full, honor the returned backpressure retry hint rather than submitting an unbounded burst.
6. Before restoring a state file, stop writers or use the deployment's coordinated maintenance procedure; verify the restored schema and run the regression suite before reopening traffic.

## 6. Security or tenant-scope incident

1. Disable the affected project/provider path immediately if a tenant/project/provider scope mismatch is detected.
2. Preserve the tenant ID, project ID, provider ID, safe denial code, and request ID; never include secrets or user content.
3. Verify the project is bound to exactly one tenant and the configured provider/modality allowlists are correct.
4. Review credential, quota, job, result, and usage storage boundaries separately. The tenant execution gate alone does not prove those stores are tenant-partitioned.
5. Rotate credentials through the approved secret-management process if exposure is suspected; do not paste secrets into issues, logs, or test fixtures.
6. Require a regression test for the cross-tenant path and verify that no adapter was invoked before restoring access.

## 7. Restore and rollback

1. Identify the last known-good code/configuration revision and the state files affected.
2. Back up the current files before restore; preserve the originals for incident analysis.
3. Restore only schema-compatible data and use the manager's atomic restore/rollback path where available.
4. Validate catalog schema, endpoint allowlists, deprecated-provider state, cost/credit/quota ledgers, queue ownership, tenant scope, and SLO definitions.
5. Run compilation, JSON/catalog validation, and the complete Ubuntu/Windows regression matrix.
6. Run an authorized, provider-specific smoke test for each affected modality before reopening traffic.
7. Record the change, approver, verification evidence, remaining limitations, and rollback trigger.

## 8. SLO and incident follow-up

- Evaluate SLOs only with sufficient samples in the configured rolling window. INSUFFICIENT_DATA is not a pass.
- Review success-rate, p95 latency, error-budget remaining, provider incidents, queue wait, and budget alerts together.
- Assign an owner and due date for each corrective action. A code fix is not complete until its regression test passes on supported platforms.
- Keep external alert delivery, production telemetry export, load testing, authorized live-provider checks, and disaster-recovery drills as separate operational gates until they have been exercised in the target environment.

## 9. Release checklist

- [ ] No unresolved security, tenant-isolation, or unexpected-spend incident.
- [ ] Provider catalog and configuration validate; deprecated providers remain disabled.
- [ ] Cost reservations and unknown outcomes have been reconciled or deliberately retained.
- [ ] Relevant unit/regression tests pass on Ubuntu and Windows.
- [ ] Authorized live connection and modality smoke tests pass.
- [ ] Output validation and data-handling policy checks pass.
- [ ] SLO/error-budget and queue/worker state are reviewed.
- [ ] Backup/restore or rollback evidence is recorded.
- [ ] Operator and approver agree that the remaining limitations are acceptable.

**Important limitation:** this runbook documents a safe procedure; it is not evidence that the procedure has been rehearsed. Production readiness requires deployment-specific owners, contact paths, alert channels, and timed incident/restore drills.
