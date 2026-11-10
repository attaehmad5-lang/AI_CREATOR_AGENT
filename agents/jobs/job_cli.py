"""Local-only CLI for the master workflow. Do not expose it as an unauthenticated API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from agents.jobs.job_engine import JobStateError, MasterJobEngine, MAX_OUTPUT_BYTES


def _read_json(path: str) -> dict:
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_OUTPUT_BYTES:
        raise JobStateError("Missing, symlinked or oversized JSON input")
    obj = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise JobStateError("JSON input must be an object")
    return obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Master job workflow control")
    parser.add_argument("--db", default="agents/jobs/master_jobs.sqlite3")
    parser.add_argument("--artifact-root", default=os.environ.get("AI_CREATOR_JOB_ARTIFACT_ROOT"),
                        help="Enable local SHA-256 media integrity verification under this root")
    parser.add_argument("--strict-production", action="store_true",
                        help="Fail closed unless signed receipts and local artifact verification are configured")
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="Create job from local JSON file")
    submit.add_argument("request_file")
    submit.add_argument("--idempotency-key", help="Stable retry key (no secrets)")
    events = commands.add_parser("events", help="Metadata-only local audit history")
    events.add_argument("job_id")
    events.add_argument("--limit", type=int, default=100)
    status = commands.add_parser("status", help="Show safe status summary")
    status.add_argument("job_id")
    worker = commands.add_parser("worker", help="Process queued jobs without external billing")
    worker.add_argument("--drain", action="store_true")
    record = commands.add_parser("record", help="Attach trusted stage evidence from JSON")
    record.add_argument("job_id")
    record.add_argument("stage")
    record.add_argument("output_file")
    record.add_argument("--signature", help="Verified HMAC receipt, required in signed mode")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id")
    retry = commands.add_parser("reopen")
    retry.add_argument("job_id")
    retry.add_argument("--reason", required=True)
    history = commands.add_parser("dispatch-history", help="Provider operation audit metadata only")
    history.add_argument("job_id")
    reconcile = commands.add_parser("reconcile", help="Store independently confirmed provider outcome")
    reconcile.add_argument("job_id")
    reconcile.add_argument("stage")
    reconcile.add_argument("operation_id")
    reconcile.add_argument("outcome", choices=("SUCCEEDED", "FAILED"))
    reconcile.add_argument("--resolver-id", required=True)
    reconcile.add_argument("--result-ref", help="Opaque identifier for an independently verified output")
    reconcile.add_argument("--signature", help="HMAC receipt from authenticated reconciliation service")
    reconcile.add_argument("--authorize", action="store_true", help="Confirm operator has separately verified provider result")
    legacy = commands.add_parser("migrate-legacy", help="Safely import old snapshots as unverified")
    legacy.add_argument("json_file")
    legacy.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        key_hex = os.environ.get("AI_CREATOR_JOB_RECEIPT_KEY_HEX")
        if key_hex:
            try:
                receipt_key = bytes.fromhex(key_hex)
            except ValueError as exc:
                raise JobStateError("Invalid receipt key environment configuration") from exc
        else:
            receipt_key = None
        engine = MasterJobEngine(args.db, receipt_key=receipt_key,
                                 artifact_root=args.artifact_root,
                                 strict_production=args.strict_production)
        if args.command == "submit":
            request = _read_json(args.request_file)
            if args.idempotency_key is not None:
                job = engine.submit_request(request, idempotency_key=args.idempotency_key)
            else:
                job = engine.create_job(request)
                engine.save_job(job)
            output = {"job_id": job["job_id"], "status": job["status"]}
        elif args.command == "events":
            output = {"job_id": args.job_id, "events": engine.job_events(args.job_id, limit=args.limit)}
        elif args.command == "status":
            job = engine.load_job(args.job_id)
            if job is None:
                raise JobStateError("Job not found")
            stage = job.get("stage_result")
            safe_stage = {k: stage[k] for k in ("stage", "status", "reason", "exception_type")
                          if k in stage} if isinstance(stage, dict) else None
            output = {"job_id": job["job_id"], "status": job["status"],
                      "current_stage": job["current_stage"], "stage_result": safe_stage}
        elif args.command == "worker":
            output = {"processed": []}
            while True:
                claimed = engine.claim_next("cli-" + uuid.uuid4().hex)
                if not claimed:
                    break
                job = engine.process_claim(claimed["job"]["job_id"],
                                           claimed["worker_id"], claimed["lease_token"])
                output["processed"].append({"job_id": job["job_id"], "status": job["status"],
                                            "current_stage": job["current_stage"]})
                if not args.drain:
                    break
        elif args.command == "record":
            job = engine.record_stage_output(args.job_id, args.stage, _read_json(args.output_file),
                                             receipt_signature=args.signature)
            output = {"job_id": job["job_id"], "status": job["status"], "current_stage": job["current_stage"]}
        elif args.command == "dispatch-history":
            output = {"job_id": args.job_id, "dispatches": engine.external_dispatch_history(args.job_id)}
        elif args.command == "reconcile":
            output = engine.reconcile_external_dispatch(args.job_id, args.stage,
                args.operation_id, args.outcome, resolver_id=args.resolver_id,
                result_ref=args.result_ref, receipt_signature=args.signature,
                authorize_reconciliation=args.authorize)
        elif args.command == "cancel":
            job = engine.cancel_job(args.job_id)
            output = {"job_id": job["job_id"], "status": job["status"]}
        elif args.command == "reopen":
            job = engine.reopen_failed_job(args.job_id, reason=args.reason)
            output = {"job_id": job["job_id"], "status": job["status"]}
        else:
            output = engine.import_legacy_jobs(args.json_file, dry_run=not args.apply)
            output["dry_run"] = not args.apply
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (JobStateError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
