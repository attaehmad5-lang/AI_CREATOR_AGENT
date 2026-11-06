# Provider Request IDs and Distributed Trace Context

**Feature:** Task 25 #73 — Unique Request ID and Trace ID  
**Implementation status:** Service-boundary UUIDv4 request generation and W3C Trace Context propagation added; remaining asynchronous/background integrations require deployment verification.

## Request identifiers

- When the caller omits request_id, ProviderService generates a UUIDv4 identifier using the operating-system-backed Python UUID implementation.
- Caller-supplied request IDs remain stable for retries and idempotent replay; they are validated against a bounded safe character set and are not silently replaced.
- Request ID and trace ID are separate identifiers. The request ID is the idempotency/correlation key for provider accounting; the trace ID joins distributed spans across services.
- The generated request ID is passed to the routing manager and provider adapter options and is included in the service response.

## W3C Trace Context

- Incoming traceparent uses the W3C version-00 format: version, 32-hex trace ID, 16-hex parent span ID, and 2-hex trace flags.
- Malformed values, unsupported versions, all-zero trace/span IDs, invalid flags, and conflicting explicit trace IDs fail closed.
- A valid incoming trace is continued with the same trace ID and a new local span ID. A new trace ID is generated when no incoming trace is supplied.
- A contextvars-backed context scopes request ID, trace ID, span ID, parent span ID, trace flags, and traceparent. It is restored on all normal and exceptional exits and is isolated across concurrent threads/async contexts.
- Helpers expose the current trace context, create child spans, extract inbound traceparent, and inject/replace a traceparent header on a copied HTTP header mapping.
- Direct route calls and full execute_routed_request calls receive IDs. The trace context remains active while the provider adapter executes, so instrumented adapters can read it and inject W3C headers without adding arbitrary kwargs to provider API payloads.
- Service-only trace fields are removed before calling ProviderRoutingManager; routing receives the request ID but not trace metadata as unsupported keyword arguments.

## Regression evidence

Tests cover UUID uniqueness, request ID validation, W3C parsing and conflict rejection, all-zero identifiers, child-span parentage, context cleanup, header injection/extraction, malformed inbound context, thread isolation, direct route propagation, and trace context visibility inside a provider adapter.

## Residual risks and production requirements

1. Third-party SDKs and background workers must explicitly call the trace-header injection helper or copy the context into their task/message envelope. Context variables do not cross process or queue boundaries automatically.
2. Existing caller-supplied request IDs remain caller-selected idempotency keys. Deployments must authenticate callers and scope idempotency records by tenant/project; UUID generation is used only when the caller omits the ID.
3. This module propagates trace context but does not implement a telemetry exporter, trace backend, sampling policy, span retention, or distributed collector.
4. Existing external provider adapters that do not instrument outgoing HTTP calls will not automatically send traceparent to the provider. Trace propagation to third-party services should be enabled only where their APIs support it and privacy policy permits it.

**Disposition:** Feature #73 remains PARTIAL until background queue/worker propagation, all outbound adapters, and production trace collection are wired and verified.
