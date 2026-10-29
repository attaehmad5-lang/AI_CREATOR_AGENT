# Provider Incident Response and Recovery Runbook

**Scope:** Task #25 provider connection/routing layer. This is an operator checklist, not proof that production drills have been performed. Execute actions only in the intended environment and follow the service owner's approval policy.

## 1. Severity and immediate safety

- **SEV-1:** suspected credential exposure, cross-tenant data exposure, unexpected paid requests, or widespread generation outage.
- **SEV-2:** one modality/provider unavailable, sustained rate limiting, queue growth, or SLO breach without evidence of data exposure.
- **SEV-3:** isolated request failures or degraded optional capability with a safe fallback available.

For suspected secret exposure or cross-tenant leakage: stop affected provider traffic using the approved feature flag/control plane, preserve logs and audit evidence, rotate credentials through the secret manager, and notify the security incident owner. Never paste credentials, prompts, personal data, or raw authorization headers into tickets or logs.

## 2. First five minutes: establish facts

1. Record incident ID, UTC start time, reporter, affected environment, tenant/project scope, modality, provider ID, and safe request/trace IDs.
2. Inspect provider diagnostics and health state using supported read-only diagnostics. Do not edit state files manually during triage.
3. Compare failure rate, latency, queue wait, timeout rate, quota responses, and health timestamps with the last known healthy interval.
4. Distinguish FAILED from EXECUTION_OUTCOME_UNKNOWN. If a provider request may have reached the remote service, retain cost/quota reservations until reconciliation; do not blindly retry or release reservations.
5. Check recent deployments/configuration changes, provider status pages, credential expiry, rate limits, DNS/TLS errors, and feature-flag changes.

## 3. Provider-specific degradation

1. Confirm provider and modality using trusted catalog/registry data; do not infer provider identity from caller-supplied metadata.
2. If a required fresh health check is unavailable, stale, malformed, or raises an error, keep the route blocked.
3. Disable the provider only through the authorized admin control. Record approver, reason, timestamp, and change reference.
4. Enable fallback only if it is healthy and satisfies the same tenant, project, region, data-handling, privacy, approval, and cost policies. Fallback is not permission to relax policy.
5. Run a non-billable or explicitly approved canary with synthetic, non-sensitive content. Verify output schema, size limits, latency, and accounting.
6. If no policy-compliant fallback exists, return a controlled degraded/unavailable response rather than bypassing the gate.

## 4. Authentication, quota, rate limit, and cost incidents

- **401/403:** verify secret reference, scope, account/project permissions, and expiry using the secret manager. Never log the credential itself.
- **429 / rate limit:** respect a validated, bounded Retry-After; use backoff and jitter. Avoid retry storms and do not retry non-idempotent/paid operations without knowing their execution status.
- **Quota exhaustion:** compare internal reservation records with provider-side usage where available. Do not raise limits or switch billing identities without approval.
- **Unknown execution outcome:** preserve reservation and mark the request for reconciliation. Reconcile against provider request IDs/usage statements where available; release only when evidence establishes that no charge/consumption occurred, or settle the confirmed charge according to the billing ledger procedure.
- **Possible duplicate charge:** stop automatic retries for the affected operation, preserve audit records, reconcile each request ID, and escalate discrepancies to the billing owner.

## 5. Tenant, privacy, and region policy incidents

1. Stop the affected route immediately if a request may have crossed tenant/project/region boundaries or violated retention/training consent.
2. Preserve minimal access-controlled evidence; do not copy content payloads into incident channels.
3. Determine whether the issue was authorization, queue/job ownership, metadata disclosure, provider routing, or persistence.
4. Do not resume until authorization and policy regression tests pass and the security owner approves recovery.
5. Assess notification and retention/deletion obligations with the privacy/legal owner; do not make unsupported assurances about deletion.

## 6. Queue and worker recovery

1. Check queue depth, oldest-job age, worker heartbeats, lease expiry, concurrency caps, and per-tenant fairness limits.
2. Confirm whether each stalled job is safe to retry and whether its previous execution could have incurred a charge.
3. Use the supported lease-recovery/requeue mechanism; do not edit job storage directly.
4. Verify attempt counts, idempotency, terminal-state persistence, and tenant-scoped visibility after recovery.
5. If queue recovery risks duplicate paid work, pause the affected class through the authorized control plane and reconcile before resuming.

## 7. Recovery and rollback gate

Do not declare recovery based on a single successful request. Require:

- root cause understood or risk contained with an owner and follow-up;
- credentials/configuration validated without exposing secrets;
- health and capability gates passing for the affected modality;
- tenant, project, region, data-handling, cost, and paid-approval policies unchanged or explicitly approved;
- targeted regression tests plus full cross-platform CI passing;
- synthetic canary success and no unexplained duplicate charges;
- queue depth and error/latency metrics returning to agreed thresholds for the agreed observation window;
- audit trail and incident timeline complete.

Rollback to last known-good code/configuration using the approved release process. Verify data/state compatibility before rollback; do not overwrite newer durable state blindly.

## 8. Incident closure and follow-up

- Record impact, start/end times, affected modalities/tenants, confirmed versus unknown charges, mitigation, rollback, evidence links, and remaining risks.
- Add regression tests for the failure mode; rerun failed CI jobs and verify the final commit, not an earlier SHA.
- Assign owners and due dates for corrective actions. Review retry/idempotency, alert coverage, runbook gaps, and missing controls.
- Remove temporary access and expire incident-only credentials or tokens.
- Mark the incident resolved only after the recovery gate is satisfied and the incident owner approves closure.

## 9. Required drill evidence (not yet implied by this document)

A production-readiness reviewer should separately record a dated, authorized drill for provider outage/fallback, expired credentials, 429 backoff, unknown paid execution outcome and reconciliation, worker lease recovery, tenant-isolation denial, rollback/restore, and secret-exposure response. Include environment, commit SHA, test evidence, observed result, and reviewer. Do not label a drill passed without executing it.
