# Provider-Specific Worker Limits and Concurrency

**Feature:** Task 25 #75 — Provider-Specific Worker Limits and Concurrency  
**Implementation status:** Cross-process provider leases and optional ProviderService enforcement added; deployment-wide configuration and production fairness/telemetry remain open.

## Limits and lease model

ProviderConcurrencyManager stores active provider execution leases in a durable, versioned state file. Each lease identifies provider, tenant, project, request, worker, opaque lease token, acquisition time, renewal time and expiry. It stores no prompt, credential, output payload or request body.

- Each provider has an explicit configured maximum, with a bounded deployment default for providers without an override.
- Acquiring a slot is atomic across processes using the shared interprocess lock and atomic fsync persistence.
- A request cannot hold two simultaneous leases for the same provider/project/tenant/request identity.
- When a provider reaches its limit, acquisition returns PROVIDER_CONCURRENCY_LIMIT_REACHED and a retry-after estimate before the provider adapter is invoked.
- Leases expire after worker failure. Long-running calls use a background heartbeat to renew the lease while the adapter is executing.
- Heartbeat loss is treated as an ambiguous execution outcome; the service does not pretend the provider slot remained protected.
- Adapter completion releases the lease. Release failure is surfaced conservatively after a started provider call; otherwise the lease expires naturally.

## ProviderService integration

ProviderService accepts an optional concurrency_manager. When configured, it acquires a provider-specific lease after routing and preflight checks but before the adapter call. A denied acquisition releases unstarted accounting reservations and returns without invoking the adapter. If request idempotency is also configured, the concurrency lease is acquired before the idempotency state is marked STARTED; adapter invocation occurs only after both controls are satisfied.

The heartbeat thread is stopped and joined before the service releases the lease, preventing a renewal/release race. The active limit is applied to every cooperating worker using the same state file and the same provider limit configuration.

## Regression evidence

Tests cover per-provider limits, independent provider capacity, duplicate active request rejection, lease renewal/release, expiry cleanup, cross-instance concurrent acquisition, long-running heartbeat renewal, malformed state, capacity bounds, and ProviderService integration proving a second adapter does not start while the provider slot is occupied.

## Residual risks and production requirements

1. The concurrency manager is optional and must be configured in every production worker. A worker that bypasses ProviderService or uses a different state store/configuration is outside the shared limit.
2. JSON-file state is appropriate for the tested single-host/shared-filesystem contract, not a substitute for a transactional distributed coordination service across regions or hosts with unreliable shared storage.
3. The default limit and provider-specific limits must be centrally configured and identical across all workers. Divergent configurations can produce inconsistent enforcement.
4. The manager does not yet implement tenant-fair scheduling, weighted priorities, global fleet limits, queue admission, or provider-specific dynamic rate limits.
5. A release failure can leave a slot occupied until lease expiry. Production needs alerts, operator visibility and a transactional shared store for high availability.
6. The heartbeat thread renews while the synchronous adapter call is active. Async task queues and remote workers need explicit lease handoff and heartbeat integration.

**Disposition:** Feature #75 remains PARTIAL until every worker shares the same deployment-wide lease backend/configuration and tenant fairness, queueing, operational alerts and multi-host behavior are verified.
