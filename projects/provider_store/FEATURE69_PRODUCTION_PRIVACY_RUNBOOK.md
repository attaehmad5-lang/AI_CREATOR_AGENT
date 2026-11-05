# Feature 69 — Production Privacy Adapter Runbook

This runbook turns the provider-store privacy lifecycle interfaces into deployable components. It does **not** provision cloud resources, create credentials, or claim a live provider deletion. A deployment is production-ready only after its own database, object-store, cache, backup, and provider adapters have been configured and deletion evidence has been captured.

## 1. Security boundaries

- Keep all KMS/secret-manager credentials and encryption keys outside the repository, CI logs, environment dumps, and application responses.
- Use separate purpose-scoped keys: AES-256 state/configuration keys, HMAC-SHA-256 idempotency/digest keys, and any provider API credential. Do not reuse a destroyable per-store DEK as the idempotency HMAC key.
- Supply stable 32-byte HMAC keys through a deployment-owned callable. The context is part of the lookup contract and must resolve to the same key after process restarts and deployments.
- Protect the application database, SQLite privacy ledger, WAL files, object stores, snapshots, backups, and generated assets with deployment storage/volume encryption controls. The JSON AES-GCM state store does not encrypt arbitrary database files or external assets.
- Run the service with least-privilege database and cloud identities. The SQLite adapter requires SELECT/DELETE on its target table and access to its two aca_privacy ledger tables.
- Treat provider-side deletion as confirmed only when the provider's documented API returns the strict confirmation contract. HTTP 202/PENDING, timeouts, malformed bodies, redirects, and unsupported APIs are not successful deletion.

## 2. Transactional SQLite adapter

SQLitePrivacyDataAdapter deletes rows from one existing application-owned SQLite table. That table must have these columns:

| Column | Requirement |
|---|---|
| subject_ref | Pseudonymous subject lookup handle; do not use email/phone as this value |
| project_id | Project boundary |
| tenant_id | Tenant boundary; SQL NULL is a separate un-tenanted scope |
| expires_at | Canonical UTC ISO-8601 text, for example 2026-11-01T00:00:00.000000+00:00 |

The adapter checks the required columns at startup, binds all values as SQL parameters, validates the table identifier, and commits row deletion and its HMAC request receipt in the same SQLite transaction. The same request ID cannot be replayed for a different subject/project/tenant/cutoff. The configured row ceiling rejects an oversized batch before deleting anything.

Example (inject the key from your KMS/secret manager; never replace this with a literal in production):

~~~python
from projects.provider_store.privacy_adapters import SQLitePrivacyDataAdapter

def privacy_hmac_key(context: str) -> bytes:
    # Deployment SDK call; fetch a stable, purpose-scoped 32-byte key.
    return kms_client.read_secret_bytes(context=context)

database_adapter = SQLitePrivacyDataAdapter(
    "/var/lib/ai-creator/application.sqlite",
    table="subject_data",
    adapter_id="application-db",
    digest_key_provider=privacy_hmac_key,
    max_deleted_rows=100_000,
)
~~~

This adapter deletes rows in that table only. It does not discover/delete referenced files, child rows in other databases, analytics copies, database snapshots, or provider-side assets. Register a separate adapter for each of those stores. Ensure expires_at values are normalized to UTC before insertion; the adapter deliberately performs a bounded lexical comparison against canonical UTC ISO-8601 text.

## 3. Provider-side HTTPS deletion

HTTPSPrivacyProviderAdapter can call a provider's documented deletion and retention endpoints. The deployment must provide exact HTTPS URLs, exact hostname allowlists, and a just-in-time authorization-header callback. The adapter rejects credentials in URLs, non-default ports, unlisted hosts, redirects, non-JSON/malformed responses, and oversized response bodies; it keeps TLS certificate verification enabled.

The provider API must accept:

- A JSON request containing only a pseudonymous subject reference and project/tenant scope for deletion, or a UTC cutoff and scope for retention.
- An Idempotency-Key request header and durable deduplication at the provider.
- A JSON response containing meaningful confirmed, status, and deleted_count fields. The adapter reduces this to the minimal status/count contract and discards other response fields.

Example:

~~~python
from projects.provider_store.privacy_adapters import HTTPSPrivacyProviderAdapter, PersistentPrivacyAdapter

provider_api = HTTPSPrivacyProviderAdapter(
    delete_url="https://api.vendor.example/v1/privacy/delete",
    purge_url="https://api.vendor.example/v1/privacy/purge",
    allowed_hosts=["api.vendor.example"],
    authorization_header=provider_credentials.authorization_header,
    timeout=5.0,
    max_response_bytes=65_536,
)

vendor_privacy = PersistentPrivacyAdapter(
    provider_api,
    ledger_path="/var/lib/ai-creator/privacy-vendor-ledger.sqlite",
    adapter_id="vendor-image-provider",
    digest_key_provider=privacy_hmac_key,
)
~~~

Do not configure these URLs until they match the provider's official privacy documentation and account permissions. If the vendor does not support idempotency or does not confirm deletion of provider-held copies, keep that adapter's operation PARTIAL; a successful HTTP response alone is not proof that all copies were erased.

## 4. Durable idempotency for external adapters

PersistentPrivacyAdapter wraps an adapter and stores HMAC-SHA-256 digests of request/scope identifiers, operation, state, attempt count, minimal status, and timestamps in a SQLite ledger. The create_privacy_lifecycle_manager factory applies this wrapper automatically to external adapters; SQLitePrivacyDataAdapter and adapters already wrapped with PersistentPrivacyAdapter are kept as-is. It does not store raw subject, project, tenant, request IDs, API credentials, or raw adapter responses. Its request lease prevents concurrent duplicate execution, and an attempt counter fences off stale workers after a lease expiry. Failed/PENDING operations can be retried with the same request ID after the lease window.

A crash can occur after a remote system deletes data but before this local ledger records completion. Therefore the remote adapter must also deduplicate the supplied request ID. Without remote idempotency, exactly-once deletion across a network boundary is impossible; retain an honest PARTIAL status and reconcile manually.

Use a durable, protected local volume for the ledger. Multiple workers may share it only where the filesystem/database provides reliable SQLite locking. For multi-region or object-store execution, replace the local ledger with a deployment-specific transactional/distributed idempotency store implementing the same claim/complete/fencing guarantees. Do not place SQLite WAL databases on a filesystem whose locking semantics have not been validated.

## 5. Register all deletion surfaces

Build the lifecycle coordinator through the registration factory so every external provider/cache/object-store adapter receives a durable per-store idempotency ledger. SQLitePrivacyDataAdapter already commits its data deletion and receipt atomically and is not wrapped a second time.

~~~python
from projects.provider_store.privacy_adapters import create_privacy_lifecycle_manager

privacy_lifecycle = create_privacy_lifecycle_manager(
    stores={
        "application-db": database_adapter,  # SQLitePrivacyDataAdapter
        "vendor-provider": provider_api,     # HTTPS / external adapter
        # Add cache, object-store, asset, backup and analytics adapters.
    },
    ledger_directory="/var/lib/ai-creator/privacy-ledgers",
    digest_key_provider=privacy_hmac_key,
)

# The factory creates a strict lifecycle manager: each initial adapter is wrapped
# with a persistent HMAC idempotency ledger unless it is already the transactional
# SQLite adapter. The returned manager rejects later registrations that are not
# already protected by the same durable-idempotency contract.
assert privacy_lifecycle.require_durable_idempotency is True
assert privacy_lifecycle.durable_idempotency_ready is True

# In production, require encrypted persistent state and a non-empty inventory
# in which every adapter has durable idempotency. Startup fails closed otherwise.
provider_service = ProviderService(
    connection_manager,
    privacy_lifecycle_manager=privacy_lifecycle,
    require_encrypted_state=True,
    require_privacy_lifecycle=True,
)
~~~

Adapters must implement both delete_subject_data(subject_ref, project_id, tenant_id, request_id) and purge_expired_data(cutoff, project_id, tenant_id, request_id). If a system is not configured or a response is PENDING/FAILED/malformed, the coordinator returns PARTIAL. Do not register an adapter that returns success without performing and verifying the operation.

### Generated-media asset store integration

The repository has a concrete local-filesystem adapter for the generation pipeline. It coordinates
`projects/generation/asset_manifest.json`, `projects/generation/generation_state.json`, and the
configured generated-media roots. Add it to the same lifecycle coordinator:

~~~python
from projects.generation.privacy_asset_adapter import GenerationAssetPrivacyAdapter

def generation_metadata_dek(context: str) -> bytes:
    # KMS/Secret Manager: return the stable, dedicated 32-byte DEK for this context.
    return kms_client.get_or_create_data_key(context=context, purpose="generation-privacy-metadata")

generation_asset_privacy = GenerationAssetPrivacyAdapter(
    "projects/generation/asset_manifest.json",
    state_paths=["projects/generation/generation_state.json"],
    asset_roots={
        "output/images": "/srv/ai-creator/output/images",
        "output/videos": "/srv/ai-creator/output/videos",
        "output/voice": "/srv/ai-creator/output/voice",
        "output/assets": "/srv/ai-creator/output/assets",
    },
    encryption_key_provider=generation_metadata_dek,
    encryption_context="generation-assets-v1",
    require_encrypted_metadata=True,
    migrate_legacy_plaintext_metadata=True,  # One-time, controlled migration only.
)

# First encrypt existing JSON metadata. This does not invent ownership.
encryption_migration = generation_asset_privacy.migrate_legacy_metadata(confirmed=True)
if encryption_migration["status"] not in {"GENERATION_METADATA_ENCRYPTED", "GENERATION_METADATA_ALREADY_ENCRYPTED"}:
    raise RuntimeError("generation metadata encryption migration failed")

# Resolve each legacy asset/job ID against the authoritative project/tenant/user database.
# The resolver returns project_id, tenant_id (including explicit None), subject_ref,
# and optionally an aware expires_at. Never use email/phone as subject_ref.
def resolve_generation_owner(lookup: dict) -> dict:
    return ownership_store.resolve_asset_scope(
        asset_id=lookup.get("asset_id"),
        job_id=lookup.get("job_id"),
        project_name=lookup.get("project"),
        scene=lookup.get("scene"),
        media_type=lookup.get("media_type"),
    )

scope_migration = generation_asset_privacy.migrate_legacy_scope_metadata(
    scope_resolver=resolve_generation_owner,
    confirmed=True,
)
if scope_migration["status"] not in {"GENERATION_SCOPE_METADATA_MIGRATED", "GENERATION_SCOPE_METADATA_ALREADY_VALID"}:
    raise RuntimeError("authoritative generation ownership migration failed")

asset_audit = generation_asset_privacy.audit()
if asset_audit["status"] != "ASSET_PRIVACY_READY":
    raise RuntimeError("generation asset privacy scope migration is incomplete")
if asset_audit["retention_missing_expiry_count"] != 0:
    raise RuntimeError("generation asset retention metadata is incomplete")

# Recreate in steady-state mode: legacy plaintext is no longer accepted.
generation_asset_privacy = GenerationAssetPrivacyAdapter(
    "projects/generation/asset_manifest.json",
    state_paths=["projects/generation/generation_state.json"],
    asset_roots={
        "output/images": "/srv/ai-creator/output/images",
        "output/videos": "/srv/ai-creator/output/videos",
        "output/voice": "/srv/ai-creator/output/voice",
        "output/assets": "/srv/ai-creator/output/assets",
    },
    encryption_key_provider=generation_metadata_dek,
    encryption_context="generation-assets-v1",
    require_encrypted_metadata=True,
    migrate_legacy_plaintext_metadata=False,
)
privacy_lifecycle = create_privacy_lifecycle_manager(
    stores={
        "application-db": database_adapter,
        "generation-assets": generation_asset_privacy,
        "vendor-provider": provider_api,
    },
    ledger_directory="/var/lib/ai-creator/privacy-ledgers",
    digest_key_provider=privacy_hmac_key,
)
~~~

The adapter uses an OS-level interprocess lock and atomic JSON replacements. It deletes only
actual `file_path` / `output_file` entries under the exact configured root prefixes; planned
output paths are never deleted. Shared paths referenced by a non-target subject block deletion.
Every row must carry a pseudonymous `subject_ref`, `project_id`, and an explicit `tenant_id`
(`None` means the no-tenant scope). The generation manifest/state builders propagate and validate
these fields from their source execution report. A partial or sensitive scope is rejected rather
than guessed.

**Legacy migration gate:** the currently committed sample generation manifest/state files predate
these fields. They are intentionally not backfilled with invented owners. Until legacy records are
re-generated with trustworthy scope metadata or migrated from an authoritative ownership source,
this adapter returns FAILED/PARTIAL and does not delete their files. Retention purge also requires
an explicit timezone-aware `expires_at` on every in-scope record; no retention date is inferred.
 
Run the metadata-only readiness audit before enabling subject deletion or retention:

~~~python
asset_audit = generation_asset_privacy.audit()
if asset_audit["status"] != "ASSET_PRIVACY_READY":
    raise RuntimeError("generation asset privacy scope migration is required")
if asset_audit["retention_missing_expiry_count"] != 0:
    raise RuntimeError("generation asset retention metadata is incomplete")
~~~

The audit reports document/record counts only; it never returns subject IDs, filenames or paths.
Subject deletion may be enabled when all rows are explicitly scoped. Scheduled retention also
requires zero in-scope records without explicit expiry metadata.

A complete deployment inventory should cover:
1. Primary application databases and replicas.
2. Object storage, generated image/video/audio assets and multipart upload remnants.
3. Caches, search indexes, vector stores and analytics/event pipelines.
4. Backups, snapshots, exports and disaster-recovery replicas under their own legal retention schedule.
5. Every downstream provider that received the subject's prompts, files, outputs or identifiers.
6. Operational logs and audit records, subject to legal/security retention exceptions and pseudonymization requirements.

Use the same pseudonymous subject handle and exact project/tenant scope consistently across adapters. The coordinator never sends an email address or phone number as a lookup key and never returns the subject reference in its result.

## 6. Scheduled retention

PrivacyRetentionJob is a one-shot worker intended to be invoked by an external scheduler (Kubernetes CronJob, systemd timer, cloud scheduler, or cron). It pins project/tenant scopes at construction, obtains a deployment-controlled service principal, computes a UTC calendar-day cutoff, and reports PARTIAL if any scope is not confirmed. When ProviderService has an OIDC identity_authenticator configured, principal_provider must return the exact verifier-issued authenticated principal capability object; the job preserves it unchanged. A caller-supplied identity string is not an acceptable substitute in OIDC mode. ProviderService performs the authoritative principal, permission and tenant/project checks.

Example inside the deployment's existing application factory:

~~~python
from projects.provider_store.privacy_adapters import PrivacyRetentionJob

retention_job = PrivacyRetentionJob(
    provider_service,
    principal_provider=load_retention_service_principal,
    scopes=[
        {"project_id": "documentary-project", "tenant_id": "tenant-a"},
        # One explicit record for each authorized project/tenant boundary.
    ],
    retention_days=90,
)

# The scheduler's job entry point invokes this once and exports only the
# returned bounded status/count metadata to the monitoring system.
result = retention_job.run_once()
if result["completed"] is not True:
    raise RuntimeError("privacy retention run was not fully confirmed")
~~~

The default run ID is deterministic for one project/tenant and UTC cutoff day, so a scheduler retry on the same UTC day reuses the same logical idempotency key. Configure the service principal with provider.access.manage and the required tenant/project grants. Never expose the worker as an unauthenticated endpoint or accept scopes/principal IDs from a public request. The deployment must persist scheduler run history and alert on PARTIAL/failed runs; this class does not create a scheduler or run in the background by itself.

## 7. Startup gates and release evidence

For provider JSON state managers, provide a deployment-owned AES-256 key callback and set ProviderService(require_encrypted_state=True). The strict startup gate requires every detected persistent manager to use the concrete EncryptedJSONStateStore and verifies the key callback at startup. Also enable active provider-configuration encryption and encrypted migration backups in ProviderConfigLifecycleManager; the JSON state gate does not automatically protect active catalog files, external DBs or assets. Plan the one-time legacy plaintext migration and remove legacy backups only after an approved, verified migration.

Before marking Feature 69 fully production-verified, attach evidence for all of the following:

- KMS/secret-manager key provisioning, least-privilege IAM, key versioning, rotation, recovery and destruction of unique per-store DEKs.
- Startup gates enabled and verified on the actual deployment: `require_encrypted_state=True` for every persistent provider manager and `require_privacy_lifecycle=True` with a non-empty, complete registered-store inventory; active provider configuration encryption must also be enabled.
- Storage-level encryption for primary DBs, privacy ledgers, object stores, backups, snapshots and generated assets.
- A complete inventory of every store/provider adapter registered in the lifecycle manager, with stable adapter IDs and persistent idempotency.
- Live deletion and expiry-purge evidence for each adapter, including provider-side confirmation, tenant isolation, retry/crash recovery, and the absence of residual referenced assets/copies.
- Scheduler execution evidence, alerting, retry/reconciliation playbooks, and an owner for PARTIAL results.
- Restore/recovery drills demonstrating that deleted data is not reintroduced from backups or replicas outside the approved retention policy.

Until this evidence exists, keep Feature 69 marked PARTIAL even if all repository tests pass. This is intentional: code-level contract tests do not prove a cloud account, database, object store, KMS key, provider endpoint, or scheduled production job was configured.
