# Provider Manager Guide (Task #25)

This guide describes the current implementation in this branch. It is not a claim that the full A–P checklist is complete.

## Runtime registry integration

The central catalog is the source of provider metadata. Runtime Python factories are a separate trusted registry: provider JSON must not supply an importable module or class name. `RuntimeProviderRegistry` has explicit built-in factories for the existing Hugging Face and Pollinations adapters and accepts additional callable factories only from trusted application code. Every load goes through `ConnectionManager.load_provider`, which checks provider existence, enabled state, and the modality contract before retaining a process-local instance.

`UnifiedProviderRegistry` exposes `load_runtime_provider(provider_id)`, `load_enabled_runtime_providers()`, `unload_runtime_provider(provider_id)`, and `runtime_status()`. The status deliberately distinguishes total catalog entries from adapters registered in this process; catalog presence or an adapter being loaded does not prove a live provider connection. Providers without a registered adapter return `ADAPTER_NOT_REGISTERED` and are not silently treated as usable.

## Provider onboarding

Call `UnifiedProviderRegistry.register_provider(provider, adapter_factory=trusted_factory)` from trusted application code. The provider metadata must pass the complete catalog schema; enabled API providers in catalog v3 require the official HTTPS documentation URL, host allowlist, and declared capabilities. Never place API keys, access tokens, or other credential values in the provider record. Omit `adapter_factory` for metadata-only registration; the registry will report `ADAPTER_NOT_REGISTERED` when that provider is later asked to load. A supplied factory is trusted executable code and is never sourced from a catalog module/class field.

## Components

- `connection_manager.py` — provider catalog discovery, registration, enabled/disabled state, callable contracts for every modality present in the 54-provider catalog (including advanced editing, upscaling, transcription, audio, story, and video-editing categories), credential-presence checks, endpoint policy, and verified connection state. Public catalog views rename legacy `status` to `catalog_status`; only a successful probe can set runtime connection state to `CONNECTED`. For API-key probes, `configure_provider(..., authentication_required=True)` must be used only for an endpoint documented to validate that credential; public model-list endpoints cannot prove authentication.
- `runtime_adapter_registry.py` — trusted, explicit provider-ID-to-factory registry that connects catalog entries to process-local runtime adapters without importing module/class names from catalog JSON. Loads delegate to `ConnectionManager` so enabled-state and modality contracts are always enforced; missing adapters remain clearly reported instead of being marked ready.
- `unified_provider_registry.py` — unified public facade for catalog listing, runtime adapter loading/unloading, enabled-provider batch loading, and registry coverage status.

- `register_provider(...)` validates the full post-registration catalog before writing, deep-copies metadata to prevent caller-side mutation, and refuses metadata overwrite while a runtime adapter is loaded. `UnifiedProviderRegistry.register_provider(provider, adapter_factory=...)` optionally binds a trusted factory after saving metadata; if the process-local factory binding fails, the return value explicitly reports `REGISTERED_WITHOUT_ADAPTER` rather than claiming a fully bound provider.
- `http_probe.py` — optional standard-library HTTPS probe for explicitly configured endpoints, with mandatory allowlist validation, TLS verification, no automatic redirects, bounded response reads, timeout propagation, provider-specific environment-only API credential headers, and safe HTTP status / `Retry-After` metadata for rate-limit retry decisions. Credentialed probes fail closed unless `authentication_required=True` is persisted for an endpoint documented to require and validate the supplied credential. A generic HTTP 2xx never proves generation capability or account credit balance.
- `oauth_pkce_manager.py` — OAuth 2.0 authorization-code + PKCE (S256) for public clients, with allowlisted HTTPS authorization/token endpoints, one-time expiring state, bounded token responses, redirect rejection, in-memory-only tokens, and explicit refresh only when a refresh token exists. It does not implement client-secret OAuth or persist tokens across restarts.
  - `begin_pollinations_authorization()` uses Pollinations' documented `enter.pollinations.ai/authorize` and `/api/oauth/token` endpoints, requires a registered exact redirect URI, reads the publishable App Key from `POLLINATIONS_APP_KEY`, and requests `profile usage`. Pollinations does not issue refresh tokens, so its session requires re-authorization after expiry.
- `routing_manager.py` — priority, free-credit-first, lowest-cost, healthiest-first, and round-robin selection; requires a verified runtime connection by default (catalog labels alone are not evidence), with an explicit catalog-only planning option; optional health, cost, credit, and quota managers; duplicate request reservations fail closed; explicit fallback behavior.
- `credit_manager.py` — stores verified free-credit snapshots from an injected provider-specific probe, filters by compatible units, and atomically reserves/commits/releases local credit usage to prevent concurrent overspend.
- `account_balance_probe.py` — bounded HTTPS JSON balance probe that requires an explicit host allowlist, environment-only credentials, a documented auth-protected endpoint, and an explicitly confirmed response field representing available credits. It rejects ambiguous key-budget fields unless the caller has verified the field semantics; no response body or token is returned.
- `cost_policy.py` — paid-approval guard, required project budgets, idempotent usage reconciliation, estimated-cost reservations, and `finalize_usage()` which atomically validates charge outcomes and idempotency keys against the reloaded ledger before committing usage.
- `quota_manager.py` — hourly/daily/monthly UTC quota windows and request reserve/commit/release.
- `health_manager.py` — bounded provider health history, staleness/latency checks, eligibility, and a bounded persisted request-ID ledger so repeated finalization cannot append duplicate health events or silently reuse an ID for a conflicting result.
- `retry_policy.py` — bounded backoff decisions that consume validated HTTP status / `Retry-After` metadata from typed provider errors, plus a concurrency-safe circuit breaker with half-open probe-slot recovery when an attempt is cancelled.
- `output_validator.py` — bounded file reads, file-mutation detection during validation, file size/extension/signature/basic-content checks, JSON depth limits, and SHA-256 for supported artifacts.
- `provider_service.py` — integration facade for API/OAuth/manual connection verification, health recording, routing, output checks, retry decisions, and usage finalization. `verify_http_provider()` wires the built-in allowlisted HTTPS probe into connection-state and health recording; `verify_oauth_provider()` passes an OAuth manager's unexpired access token through an ephemeral environment mapping without persisting it. Configure `probe_endpoint` separately from `endpoint` to verify an auth-protected, non-billable account endpoint without replacing the generation endpoint.
- `provider_catalog.json` and `provider_catalog_master.json` — one synchronized 54-provider registry, including the 18 advanced-provider records. Catalog v3 requires complete provider identity fields and explicit `enabled` / `subscription_required` booleans; provider IDs and documentation/subscription links are validated before acceptance. The master catalog is the fallback copy, and the older v2 backup remains archival.
- Legacy store, category, advanced, unlock, status, and unified-registry/router facades now read the central catalog. Discovery facades distinguish catalog labels from live verified state; actual policy-aware execution still belongs to `ProviderRoutingManager`.

## Safety contracts

1. **No automatic purchases.** These modules do not purchase credits, start subscriptions, or infer paid approval.
2. **Paid usage requires both explicit provider approval and a configured project budget.** Paid project budgets and estimates are USD-denominated in this implementation; paid routing rejects other cost units. Reserve the estimate before execution. Zero-cost routes are allowed only when the provider catalog explicitly sets `zero_cost_allowed: true`; a zero estimate alone is not proof that a provider is free. After an actual provider request starts, reconcile the actual cost and commit its quota even if the generation failed.
3. **Free credits must be evidence-based and unit-compatible.** Configure `ProviderCreditManager` with an injected, provider-specific account-balance probe. The probe must return a verified balance, source, and unit; it may include the provider's observation time, otherwise the manager records the local observation time when the probe returns. The probe must query a trusted provider account source rather than reuse an old cached balance. Routing must not compare unlike units (for example, provider credits against a USD estimate). Free-credit routes require an idempotency/request ID and reserve credits before routing; after the provider request starts, commit the reservation unless the provider confirms credits were not consumed. Release only when the request is known not to have started or could not consume credits. Refresh is blocked while reservations are in flight and cannot erase local usage with a snapshot observed at or before the latest local debit, and snapshot timestamps cannot regress. Direct `ProviderCostPolicy` calls and caller-supplied numeric maps cannot authorize free usage; only the routing path with its injected credit manager may do so. No generic injected probe is proof of a live provider account balance.
4. **Do not release reservations just because a provider failed.** A failed API request may still consume quota or cost. `select_fallback` keeps reservations by default and requires a new attempt ID when a previous attempt may have started. Set `release_reservations=True` only when the caller knows the provider request did not start or could not be billed.
5. **Connectivity checks are injected.** `ConnectionManager.verify_connectivity` passes a timeout to the probe, but arbitrary Python callbacks cannot be forcibly stopped if they ignore it. Each provider-specific probe must enforce its own network timeout. Credentialed generic HTTP probes require `authentication_required=True` only when official provider documentation confirms that endpoint validates the credential. Account-based providers must use `verify_manual_connection` with a live, user-authorized session; the session object is never persisted, and the injected probe must actually validate it. OAuth PKCE tokens and pending verifiers stay in memory only; token refresh is explicit, and providers that issue no refresh token require a new authorization flow. Routing requires a persisted `CONNECTED` state with `verified=true` by default; `require_verified_connection=False` is intended only for explicit catalog/planning workflows, not real execution.
6. **Output validation is not full decoding.** Signatures and basic content checks detect many mismatches, but do not replace media decoding, malware scanning, or semantic quality checks.
7. **Persistence limits.** Shared JSON state for provider connection/catalog, health, credits, cost budgets, and quotas uses atomic replacement plus OS-level interprocess lock files and reload-before-mutate. This prevents lost read-modify-write updates across local processes when every writer uses these manager APIs. It is not a single transaction across multiple manager files, and it does not coordinate separate devices, containers with non-shared lock semantics, or external writers bypassing these APIs. Routing compensates reservations created by the current attempt when a later quota step rejects the route, and fails closed if that compensation fails; this reduces orphaned reservations but does not make the separate JSON files one atomic transaction. SQLite or a transactional database is preferred for production multi-worker coordination.
8. **Secrets stay outside the catalog.** Store environment-variable names or references, not credential values. Returned provider metadata and state views redact credential-like fields.

## Explicit catalog recovery

`ConnectionManager.restore_catalog_from_master()` restores the provider catalog only when the target is missing or fails validation. It validates the master copy first, refuses to overwrite an already-valid target, uses the target's interprocess lock, and reports a safe status instead of silently replacing a valid catalog.

Numeric request values are normalized with overflow-safe finite-number checks before they reach cost, credit, routing, connection, health, retry, OAuth, and account-balance policies.

Run from the repository root:

```python
from projects.provider_store.connection_manager import ConnectionManager

result = ConnectionManager.restore_catalog_from_master()
print(result)
```

This restores catalog metadata only; it does not restore credentials, connection sessions, quota ledgers, or credit/cost history.

## Verification

The GitHub Actions workflow compiles provider modules/tests, parses provider JSON catalogs, and runs all unit/regression tests with Python 3.13:

```bash
python -m compileall -q projects/provider_store tests
python -c "import json; from pathlib import Path; [json.loads(p.read_text(encoding='utf-8')) for p in Path('projects/provider_store').rglob('*.json')]"
python -m unittest discover -s tests -p "test_*.py" -v
```

For a local Hugging Face credential check, set `HF_TOKEN` in the process environment (never paste it into source code or print it), then run this from the repository root. It checks the documented authenticated identity endpoint, not generation credits or model availability:

```python
import os
from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_service import ProviderService

manager = ConnectionManager()
configured = manager.configure_provider(
    "huggingface",
    probe_endpoint="https://huggingface.co/api/whoami-v2",
    authentication_required=True,
)
if configured.get("saved") is not True:
    print(configured.get("status", "CONFIGURATION_FAILED"))
else:
    result = ProviderService(manager).verify_http_provider(
        "huggingface", environ=os.environ
    )
    print({
        "status": result.get("status"),
        "verified": result.get("verified"),
        "health": (result.get("health") or {}).get("status"),
    })
```

This probe does not display the account response body or token. Do not run it in CI with a personal token; execute it only in the user's local authorized environment.

Live provider API/account probes, provider-specific OAuth registration/token-refresh integrations, complete multimodal adapters, full media decoding and semantic validation, end-to-end generation execution, provider-specific network-failure injection, cross-manager atomic transactions, and individual evidence-based verification of every item in the canonical feature checklist remain separate requirements. Fine-grained capability discovery uses only explicitly declared capability strings; a category label does not prove every advanced capability.
