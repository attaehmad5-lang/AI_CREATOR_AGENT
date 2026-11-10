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

Local test command (Python 3.11+; no credentials) exercises 36 offline tests,
including concurrent idempotent submissions, stale lease rejection, worker
crash recovery, an immutable terminal state, direct-save forgery attempts,
credential key screening, audit log privacy and CLI behavior. These tests are
not proof that third-party providers are working. Linux/Windows CI is configured
with Python 3.11 and 3.13; **only GitHub's actual run results** may establish
platform PASS. For critical deployments, test separately on physical Android/iOS
hosts, use authenticated multi-tenant service APIs, independent artifact
integrity verification, signed provider callbacks, backup/restore drills,
monitoring and a server-grade database when deploying across hosts.
