# Provider Execution Idempotency and Duplicate Prevention

**Feature:** Task 25 #74 — Idempotency and Duplicate Prevention  
**Implementation status:** Durable opt-in service execution claims and at-most-once adapter invocation guard added; provider-side idempotency semantics and deployment activation remain open.

## Request identity and fingerprint

ProviderService already uses request IDs for cost, credit, quota and job reservations. The new ProviderRequestIdempotencyManager adds a durable execution-level claim independent of whether those accounting managers are configured.

The manager scopes each key to tenant (when present), project and request ID. A SHA-256 fingerprint is computed from canonical request metadata, adapter identity, route constraints and adapter options. Only the digest is persisted; prompts and request payload values are not stored in the idempotency ledger. Reusing a key with a different fingerprint returns REQUEST_ID_CONFLICT.

## State machine

- RESERVED — one worker owns a bounded execution lease; a second caller receives REQUEST_IN_PROGRESS.
- STARTED — the service is immediately about to invoke the provider adapter. This state is never automatically reclaimed.
- COMPLETED — the provider attempt reached a terminal service result. Replays return DUPLICATE_REQUEST and do not call the adapter again.
- NOT_STARTED — the adapter explicitly confirmed that no provider request began. The same key/fingerprint may be safely reserved again.
- UNKNOWN — an adapter exception or inconsistent start/charge result means the provider may have received the request. Replays are blocked with EXECUTION_OUTCOME_UNKNOWN and require reconciliation rather than an automatic retry.

The manager uses an interprocess lock, atomic fsync persistence, bounded state, lease-token validation, tenant-scoped status reads, strict schema validation and idempotent finalization. Expired RESERVED leases may be reclaimed; STARTED and UNKNOWN states are never reclaimed merely because a timer expired.

## Service integration

ProviderService accepts an optional request_idempotency_manager. When configured, a decorator reserves the request before service execution and fingerprints the request without storing raw payloads. The service marks the request STARTED immediately before the adapter call. The wrapper finalizes the record based on the observed execution result, preserving UNKNOWN for ambiguous outcomes and allowing a retry only when the adapter explicitly reported that it did not start.

The provider adapter continues to receive request_id as its idempotency/correlation value. Provider-specific idempotency headers or keys must be added by an adapter only when the provider documents support for them; this manager does not claim that every external provider honors request IDs.

## Regression evidence

Tests cover duplicate/completed replay, request-fingerprint conflict, concurrent reservation, interprocess/shared-instance state, expired pre-start lease recovery, stale lease rejection, safe retry after confirmed not-started, permanent blocking of STARTED/UNKNOWN, tenant scoping, corrupt state, capacity limits, and ProviderService integration proving that ambiguous provider calls are never invoked twice.

## Residual risks and deployment requirements

1. The manager is opt-in and must be wired to durable storage in every production ProviderService instance. If omitted, the existing cost/quota idempotency controls remain the only active protection.
2. STARTED/UNKNOWN records require an operator/provider reconciliation workflow. This implementation intentionally does not guess whether a provider charged or completed a request.
3. Provider-side idempotency behavior differs by API. Adapter-specific idempotency header names, retention windows and replay semantics require authoritative provider documentation and live non-billable tests.
4. The JSON journal has a finite record cap and no automated compaction/archive policy. Production should move high-volume multi-worker workloads to a transactional database with tenant-aware uniqueness constraints and retention controls.
5. The manager prevents duplicate adapter invocations inside a correctly configured service boundary; it cannot prevent duplicate provider execution caused by an SDK's hidden retries, an external client bypassing ProviderService, or a separate deployment with an unshared ledger.

**Disposition:** Feature #74 remains PARTIAL until the manager is enabled in every deployment, provider-specific idempotency contracts are verified, and unknown-outcome reconciliation and durable high-volume storage are operational.
