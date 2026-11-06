# Provider Routing, Credential and Approval Audit Trail

**Feature:** Task 25 #72 — Routing, Credential and Approval Audit Logs  
**Implementation status:** Repository-level audit journal and selected service-boundary integrations added; deployment anchoring and complete event coverage remain open.

## Security goals

- Record who attempted a protected provider operation, what operation was requested, which provider/tenant/request it concerned, and whether authorization and execution succeeded.
- Record routing attempts and selected-provider outcomes without storing prompts, request bodies, credentials, raw endpoint URLs, or arbitrary caller-supplied metadata.
- Record credential-rotation outcomes without persisting secret values, secret references, or idempotency keys.
- Make unauthorized modification, insertion, deletion, reordering, and truncation of retained events detectable when the journal is verified against its externally supplied integrity key and expected history.
- Deny protected audit reads unless the caller is authorized for provider.audit.read.

## Event journal

ProviderAuditLog persists a versioned JSON journal. Each event includes a monotonic sequence, timestamp, allowlisted event type and outcome, optional actor/tenant/provider/request/trace identifiers, bounded allowlisted details, previous-event hash, and HMAC-SHA256 event authentication tag.

The HMAC key is supplied to the manager by the deployment. It must be at least 32 bytes and must be held by a secret manager or equivalent external secret store; it is never serialized into the journal or committed to source control. The journal is verified before every append. Any invalid event, sequence, previous hash, HMAC, or schema causes fail-closed behavior instead of appending to a compromised chain.

Persistence uses a process/thread lock, atomic same-directory replacement, fsync, restrictive file permissions where supported, a bounded event count, and bounded pagination. The lock file is intentionally persistent.

## Integrated service boundaries

- _authorize_operation emits an authorization_decision event for allowed and denied service authorization decisions. If the configured audit journal cannot persist the decision, the protected operation is denied.
- route_request persists a routing-attempt event before invoking the routing manager and a decision event after the result. If the pre-event cannot be written, routing is not invoked. If the result event cannot be written after routing, the response explicitly reports that routing side effects may already exist.
- Provider configuration, provider enablement, role assignment, provider-scope updates, and principal disablement emit operation-result events. Their authorization decisions are separately captured.
- Credential rotation records an attempt and successful completion. Secret values, secret references, and rotation idempotency keys are excluded.
- read_provider_audit_events verifies the journal and requires the provider.audit.read authorization operation before returning a page.

## Privacy and event schema

Only the following detail keys are accepted: operation, status, reason_code, billing_mode, model, endpoint_host, estimated_cost, actual_cost, latency_ms, approval_id, approval_status, resource_count, deleted, failed, and error_type. Numeric amounts/counts are validated and bounded by type and finite-number rules. Unknown keys are rejected rather than silently copied.

The event schema deliberately excludes prompts, media payloads, raw HTTP headers, authorization values, access tokens, API keys, passwords, secret references, OAuth codes, refresh tokens, request bodies, and raw endpoint URLs.

## Verification

Regression tests cover HMAC/hash-chain verification, event tampering, previous-hash tampering, malformed event types, identifier/detail bounds, secret-field rejection, pagination, multi-instance append, authorization decision logging, routing pre-audit fail-closed behavior, configuration/role event recording, credential rotation redaction, and admin-only integrity-checked audit reads.

## Residual risks and production requirements

1. This is tamper-evident, not intrinsically immutable. An attacker who can replace the journal and obtain the HMAC key can forge a new chain. Production must keep the HMAC key separate from the journal host and periodically anchor signed chain heads to an independent append-only/WORM or remote audit service.
2. File permissions and local locks do not replace protected storage, encrypted backups, access monitoring, or centralized log retention.
3. Service integrations cover the listed operations. Direct manager calls, external control planes, future endpoints, provider SDK callbacks, and infrastructure changes outside ProviderService need their own audit hooks.
4. Some operations can complete even if the post-operation event write fails. The service returns an explicit audit-persistence failure and the prior attempt event remains evidence; operators must reconcile such cases.
5. Retention periods, legal hold, export, incident response, and regional data residency must be configured by deployment policy. This implementation does not claim a production WORM archive or regulatory certification.

**Disposition:** Feature #72 remains PARTIAL until every provider/approval/credential control plane is covered and independent immutable audit anchoring, backup, access control and retention are evidenced in the deployment.
