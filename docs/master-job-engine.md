# Master Job Engine 2.1 — hardened local workflow core

The engine orchestrates job stages, persistence, worker leases, cancellation,
manual recovery and explicit artifact intake. It deliberately **does not** make
provider requests, spend credits, synthesize missing artifacts or report a
complete video merely because planning finished. Run live provider generation
and media verification in authenticated, separately tested integrations.

## State and contracts

- `agents.jobs.job_engine.MasterJobEngine` defaults to a local SQLite database at
  `agents/jobs/master_jobs.sqlite3` with atomic transactions, WAL, file mode
  `0600` on newly created POSIX databases, bounded JSON input, state revisions,
  priority scheduling, bounded queue size, concurrency limits and hashed worker
  lease tokens. Do not commit the generated database or WAL files to Git.
- `submit_request(request, idempotency_key="stable-request-id")` atomically
  deduplicates retries across threads/processes. Conflicting reuse fails closed.
  Keys must be non-secret, stable ASCII identifiers; only SHA-256 key hashes are
  stored. The request plus priority must match. `create_job`/`save_job` remain
  available for the legacy orchestrator, but `save_job` now only persists
  **CREATED** pre-execution inputs and cannot forge stage/output success.
- A metadata-only SQLite `job_events` journal captures writes, claims, lease
  expirations and cancellation requests without recording prompts, outputs or
  worker lease tokens. `job_events(job_id, limit=100)` is a trusted local
  operator API, not a multi-tenant public endpoint. External audit export must
  apply its own retention policy and access control.
- Credential-like JSON **field names** are rejected recursively at persistence
  boundaries; still do not send credentials in freeform topic, prompts or
  outputs. No content scanner can guarantee detection of every secret.
- Lifecycle state is `CREATED`, `QUEUED`, `RUNNING`, `WAITING`, `COMPLETED`,
  `FAILED`, or `CANCELLED`. Pipeline stage is separately recorded in
  `current_stage` (`CREATED` → `PLANNING` → `SCRIPT` → `SCENES` → `GENERATION` →
  `AUDIO` → `SUBTITLES` → `EDITING` → `QUALITY_CONTROL` → `COMPLETED`).
- `WAITING` means a required actual artifact or trusted QC evidence is missing,
  **not** successful completion. Explicit upstream `FAILED`/`REJECTED` makes the
  current stage fail; an operator must manually reopen and submit corrected
  evidence. No implicit retry dispatches a charged provider operation.
- `record_stage_output(job_id, stage, dict)` accepts **trusted** stage outputs
  only for the current inactive stage. Duplicate identical outputs are
  idempotent. An external API wrapping this method **must** authenticate and
  authorize the caller/tenant; this local module is not a public API.
- For higher-assurance deployments, configure `MasterJobEngine(receipt_key=...)`
  with at least 32 random secret bytes (provided out-of-band). Every external
  stage notification must then supply an HMAC-SHA256 `receipt_signature` using
  `stage_receipt_signature(job_id, stage, output, key)`. Proof binds both job
  identity and stage name to canonical output JSON and is never stored in the
  job database. CLI may load the same bytes from environment variable
  `AI_CREATOR_JOB_RECEIPT_KEY_HEX` and accepts `record ... --signature <hex>`;
  do not pass secrets on the command line. HMAC authenticates the producer,
  **not** physical artifact integrity; validate actual media independently.
  Signed mode intentionally disallows loading an unsigned `script_result` through
  the legacy `save_job` compatibility path; use signed SCRIPT stage receipts.
- The script stage accepts `script_result.script` as a non-empty text/list,
  and scenes as a non-empty list of scene dictionaries. Generation expects one
  `{scene, result: {status: "COMPLETED", asset_ref: "..."}}` per scene. The
  audio, subtitles and editing stages require `status: COMPLETED` and an artifact
  reference. QC expects `{status:"PASSED", checks:{integrity:true,
  visual:true, audio:true, subtitles:true, render:true, sync:true}}` with checks
  adjusted for disabled stages. The last stage requires an actual final artifact
  reference. **References and QC assertions are supplied by trusted upstream
  services; this core cannot establish their physical integrity on its own.**
- Optional production flags in `brain_analysis.production_plan` allow stages to
  be skipped. Quality control is never silently skipped. A text-only job without
  editing must submit `final_result.asset_ref` at the `COMPLETED` stage.

## Offline exercise (no API calls or credits)

```bash
python -m unittest discover -s tests -p 'test_master_job_engine.py' -v
python -m compileall -q agents/jobs tests/test_master_job_engine.py
```

Example request file `/tmp/request.json`: `{"topic":"Space documentary"}`.

```bash
python -m agents.jobs.job_cli submit /tmp/request.json --idempotency-key stable-request-id
python -m agents.jobs.job_cli worker
python -m agents.jobs.job_cli status <job-id>
python -m agents.jobs.job_cli events <job-id> --limit 25
```

The first worker pass stops at `SCRIPT` with status `WAITING` until a real
script result is provided. Provide trusted JSON output, then continue:

```bash
python -m agents.jobs.job_cli record <job-id> SCRIPT /tmp/script-output.json
python -m agents.jobs.job_cli worker
```

Further evidence is required at each enabled stage; no fake success is created.
The `status` command intentionally hides request/topic and nested result details.
The `events` command contains stage/status metadata only. Commands must run
under an appropriately restricted local service account.
For a failed job, use `reopen <job-id> --reason "operator incident review"`,
then submit corrected stage evidence. `cancel <job-id>` cancels queued jobs at
once and running jobs at the next safe checkpoint. `worker --drain` processes
all currently runnable jobs; workers exit when no runnable jobs remain. Schedule
workers via a service manager if continuous processing is needed. Do not expose
this CLI directly to untrusted users.

## Old jobs.json import

The historical JSON file remains untouched. Because old jobs had misleading
`COMPLETED` markers, migration always restarts them at `CREATED` and verifies
real outputs again. Import is explicitly opt-in:

```bash
python -m agents.jobs.job_cli migrate-legacy agents/jobs/jobs.json
python -m agents.jobs.job_cli migrate-legacy agents/jobs/jobs.json --apply
```

The default command is a dry run. Records with missing/invalid IDs or requests
are counted as invalid; existing IDs are skipped. Preserve a backup of the
legacy JSON and SQLite database before enabling a migration in production.

## Operational constraints / integration gates

This module is a reliable **local workflow core**, not an already deployed
end-to-end content creation service. Before a production-ready sign-off, connect
separate authenticated generation/audio/subtitle/render adapters, verify
provider-side artifacts and checksums, integrate application-level tenant
authorization, run live sandbox integration tests on Android/iOS as required,
and obtain actual CI PASS on Linux/Windows. Local tests require no credentials.
For multi-host deployments replace local SQLite with a server-grade
transactional data store and maintain the same lease/compare-and-set contracts.

## High-assurance acceptance and limitations

Local test command (Python 3.11+; no credentials) exercises 40 offline tests,
including concurrent idempotent submissions, stale lease rejection, worker
crash recovery, an immutable terminal state, direct-save forgery attempts,
credential key screening, signed stage receipts, audit log privacy and CLI behavior. These tests are
not proof that third-party providers are working. Linux/Windows CI is configured
with Python 3.11 and 3.13; **only GitHub's actual run results** may establish
platform PASS. For critical deployments, test separately on physical Android/iOS
hosts, use authenticated multi-tenant service APIs, independent artifact
integrity verification, signed provider callbacks, backup/restore drills,
monitoring and a server-grade database when deploying across hosts.

## v2.2 advanced handoff: physical artifact integrity (opt-in)

Use `MasterJobEngine(storage_path=..., artifact_root="/private/media", receipt_key=...)`
(or `job_cli.py --artifact-root /private/media ...`) to require **local** media
proof at every enabled media stage. The artifact root must exist, be a directory,
and be writable only by trusted services. Remote URLs and references alone do
**not** prove physical media; a separate, authenticated ingestion adapter must
fetch approved provider outputs, virus-scan and store them under this root.

A trusted producer may create a manifest via
`ArtifactVerifier(root).inspect("scene-1.png", "image/png")` and include the
result as `evidence` alongside `status:"COMPLETED"` and `asset_ref`.
`evidence` contains **exactly** `path`, `media_type`, `size_bytes` and `sha256`.
The engine streams bytes for SHA-256 verification, checks maximum size and
container signatures, rejects absolute paths, traversals and symlink entries,
and rechecks prior artifacts at QC and final handoff. Signed receipts still
bind stage output and job identity. On POSIX, safe directory-descriptor opens
prevent symlink swaps; protect the root with appropriate Windows ACLs.

Stage-specific media types: `GENERATION` images (PNG, JPEG, WebP) or videos
(MP4, WebM); `AUDIO` WAV or MP3; `SUBTITLES` VTT, SRT or plain text;
`EDITING` MP4 or WebM. The final stage rechecks the render or requires
separate verified `final_result` evidence for non-video pipelines.
Missing proof causes WAITING, mismatched, corrupt or inaccessible proof causes
FAILED, and failure requires explicit operator intervention. Existing remote
reference workflows retain their old semantics when `artifact_root` is unset.

**Limits:** container magic checks plus SHA-256 are not media decoding,
malware detection, copyright or perceptual-quality assurance. They prove that
local file bytes match the submitted hash and a basic media header. Dedicated
sandboxed media decoders and human/automated QC must still issue authenticated
stage receipts; an unauthenticated `checks: true` payload is not independently
credible. Do not market this alone as a verified or production-rendered video.

## v2.2 external provider submission: at-most-once local dispatch

`dispatch_external_stage(job_id, stage, operation_id, capability, payload,
authorize_spend=True)` is the explicit integration point for a configured
`ExecutionGateway`. It only accepts a **WAITING** job at the named stage and
supported capability. Callers must be authenticated and separately authorized
for account/tenant/budget use. The default rejects all external calls;
`execute_capability` is deliberately disabled because it had no durable record.

Before making an external request, the engine commits a unique operation intent
to SQLite and stores only stage, capability and a payload SHA-256. Provider
exceptions, timeouts and process crashes are treated as **ambiguous**: the same
job/stage/operation ID cannot be dispatched twice, even after restart. Successful
returns are `RECONCILIATION_REQUIRED` (never automatically COMPLETED) because
users must independently collect the provider's artifact and signed stage
receipt. Use `external_dispatch_history(job_id)` to review metadata safely.
For each image scene choose a stable distinct `operation_id` such as `scene-1`.

**Important:** at-most-once local dispatch does not provide exactly-once
provider-side execution. Reconcile provider-generated requests and expenses
via the external provider's API or dashboard before manual resubmission; never
blindly retry an unknown paid operation. Durable provider-specific idempotency
keys, account-level quota/budget enforcement, live integration tests and
multi-tenant authorization remain external service responsibilities. No live
provider calls are made in the offline regression suite.

### Offline regression

```bash
python -m compileall -q agents/jobs tests/test_master_job_engine.py tests/test_master_artifact_integrity.py
python -m unittest discover -s tests -p 'test_master*.py' -v
```

The 54 offline tests include SQLite lease recovery, signed receipts,
physical media hash tampering, path traversal/symlink attempts, false-success
prevention, deliberate paid-operation gating, and cross-restart replay safety.
The GitHub matrix still needs actual hosted-runner execution for Ubuntu,
Windows, Python 3.11 and 3.13. Cross-platform AST parsing is not a substitute.

### Optional synthetic media acceptance with an actual decoder

If trusted local `ffmpeg` and `ffprobe` executables are installed, run
`python -m scripts.offline_media_acceptance`. It creates a tiny valid PNG,
WAV, WebVTT and MP4 under a private temporary directory, decodes the image,
audio and video using ffmpeg, streams SHA-256 evidence, signs each required
stage receipt with an ephemeral test key, and validates end-to-end workflow
completion. This uses **zero cloud API credits**. It is intentionally separate
from the dependency-free Windows/Linux unittest matrix because ffmpeg is not
installed identically on every runner. A successful synthetic test is **not**
a production-provider connection test, a codec matrix, or a multimedia quality
certification.

## v2.3: authenticated reconciliation ledger and production safeguards

`MasterJobEngine` v2.3 adds an **atomic, backward-compatible** SQLite schema
migration from `user_version=2` to `3`. It preserves all jobs and existing
external operation intents, even when a process restarts after migration.

Local operator mode (or a separately authenticated service layer) can call
`reconcile_external_dispatch(job_id, stage, operation_id, outcome,
resolver_id=..., authorize_reconciliation=True, result_ref=...)`, where
`outcome` is `SUCCEEDED` or `FAILED`. An unknown operation, invalid output
reference, missing authorization, forged receipt, or conflicting response is
rejected without mutating existing decisions. Identical replay produces
`ALREADY_RECONCILED`. The only stored resolver identity and result reference
are **SHA-256 digests**, not raw tokens or URLs. A confirmation never marks a
job `COMPLETED` or bypasses media verification and independent stage receipts.

If `receipt_key` is configured, callers must supply a valid domain-separated
`external_reconciliation_signature(job_id, stage, operation_id,
request_sha256, outcome, result_ref, receipt_key)`. The request hash must be
read from the original immutable dispatch history, not an operator-generated
replacement. Independent proof of provider identity, actual billing and
physical media still belongs to a trusted ingestion and authorization service.

`max_external_operations=128` by default bounds the number of *distinct*
external provider operation intents per job. This is **not** a monetary budget
or rate limit. `authorize_spend=True` remains required for *every* dispatch;
no failed or ambiguous operation is ever automatically submitted again.

High-assurance local deployments should explicitly choose
`MasterJobEngine(receipt_key=secrets.token_bytes(32), artifact_root=path,
strict_production=True)`. Strict mode refuses to initialize without both
signatures and physical media verification. Never ship development's permissive
legacy mode as an authenticated production API. In the CLI, set the secret
`AI_CREATOR_JOB_RECEIPT_KEY_HEX` and `--artifact-root` before using
`--strict-production`. Store keys using managed secret storage, not in git,
command history, or crash logs.

Offline operator commands, only from a trusted account:

```
python -m agents.jobs.job_cli dispatch-history <job_id>
python -m agents.jobs.job_cli reconcile <job_id> GENERATION scene-1 SUCCEEDED \
    --resolver-id trusted-operator --result-ref asset:verified --authorize
```

In signed mode, also provide `--signature` computed by the authorized callback
service. Do not attempt to claim success solely from a `QUEUED` or `ROUTABLE`
response. Never expose these commands to untrusted clients; tenant-scoped
permissions, billing reconciliations, provider callback verification and
monitoring remain integration work.

### v2.3 offline verification

```
python -m compileall -q agents/jobs tests/test_master_job_engine.py \
    tests/test_master_artifact_integrity.py tests/test_master_reconciliation.py
python -m unittest discover -s tests -p 'test_master*.py' -v
python -m scripts.offline_media_acceptance
```

The additional suite tests signed and unsigned reconciliation, immutable
conflicting reports, concurrency races, v2→v3 schema migration, spending limits,
strict production initialization, a provider callback arriving before the original
HTTP call returns, and private ledger fields. Confirmed decisions are never
silently overwritten by a late upstream response. The final synthetic
media command requires separately available FFmpeg/FFprobe. Linux-host tests
are **not proof of Windows, Android, iOS or real third-party provider PASS**.

**Latest offline regression:** 64 tests PASS under Python 3.13.5 on Linux, plus
FFmpeg/FFprobe synthetic media PASS. Branch coverage for the two core modules
is 85% on this suite. These checks do not replace production tenant access
control, provider sandbox invoices, hosted Windows/Linux runners or physical
mobile-platform acceptance tests.


## v2.4: signed disaster-recovery snapshots, verified restore and FIFO defense

The online SQLite backup API now provides a **WAL-consistent** durable snapshot.
A manifest binds SHA-256, byte count, schema version 3, table row counts and
creation time. Production backups require an HMAC-SHA256 of the manifest with
an independent 32+ byte key. **The HMAC key must be stored out of band**. The
manifest is published only after the SQLite integrity check succeeds. Concurrent
backup attempts fail closed without replacing another process's successful copy.

Set `AI_CREATOR_BACKUP_KEY_HEX` to at least 32 random bytes in a restricted
operator environment, not in commands, source, CI logs or the repository. Then:

```bash
python -m agents.jobs.disaster_recovery backup agents/jobs/master_jobs.sqlite3 /secure/backups/master-2026-11-10.sqlite3
python -m agents.jobs.disaster_recovery verify /secure/backups/master-2026-11-10.sqlite3
python -m agents.jobs.disaster_recovery restore /secure/backups/master-2026-11-10.sqlite3 /secure/restore-rehearsal.sqlite3
```

The backup file and its `.manifest.json` are created with mode 0600 on POSIX.
Both backup and restore are **create-only**; an existing destination is never
overwritten. All temporary files and the snapshot live on the same volume;
filesystem hard links must be supported (otherwise creation fails closed).
Restore validates the signature, manifest, physical file bytes, SQLite schema
and integrity **before** copying and independently verifies copied bytes and
SQLite integrity again. Run an offline restore rehearsal and inspect its contents
before considering a backup operationally ready. Restore does **not** write into
production databases, does not contact external providers, and cannot recreate
external media, API secrets, provider billing records or remote assets.

The backup **contains sensitive job payloads** even though the manifest only
contains safe counts. Place it on access-controlled, encrypted storage with
a documented retention/rotation policy. A local HMAC does not replace backup
encryption, independent key escrow, tenant authorization or off-site replication.
The `--allow-unsigned` operator option is for throwaway offline development only.
Do **not** enable it for production. SQLite remains a single-host state store;
a clustered service needs a server-grade database and separate disaster recovery.

Artifact verification now opens regular files non-blockingly before `fstat` to
prevent FIFO/special-file denial of service on POSIX. It still checks `fstat`
for regular files and uses secure directory descriptor traversal. This check
does not provide a media decoder; validate file quality independently.

```bash
python -m unittest discover -s tests -p 'test_master*.py' -v
python -m compileall -q agents/jobs tests/test_master_disaster_recovery.py
```

The 14 additional offline tests cover WAL-consistent backup/restore, full event
retention, signed and unsigned policy, corruption, manifest forgery, wrong keys,
create-only safety, malformed databases, migration-version rejection, symlink
refusal, concurrent same-name snapshot attempts, redacted CLI output and FIFO
protection. No live cloud provider or payment APIs are invoked.

**Remaining external release gates:** actual Ubuntu/Windows CI runner PASS,
end-to-end authenticated provider staging/sandbox exercises, tenant-scoped API
authorization, independent media decoding and iOS/Android host verification.
Do not infer any of those passes from local tests.
