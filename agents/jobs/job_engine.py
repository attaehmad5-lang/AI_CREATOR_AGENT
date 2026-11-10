"""Durable, fail-closed master workflow orchestrator.

The engine orchestrates externally produced artifacts; it never bills providers or
assumes that a plan is an artifact. Workers may submit verified output references
via ``record_stage_output`` and resume a waiting job. SQLite transactions and
hashed lease tokens protect state from concurrent workers and stale completions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifact_integrity import ArtifactIntegrityError, ArtifactVerifier


STAGES = (
    "CREATED", "PLANNING", "SCRIPT", "SCENES", "GENERATION", "AUDIO",
    "SUBTITLES", "EDITING", "QUALITY_CONTROL", "COMPLETED",
)
TERMINAL = frozenset(("COMPLETED", "FAILED", "CANCELLED"))
RESULT_KEYS = {
    "SCRIPT": "script_result",
    "SCENES": "scene_result",
    "GENERATION": "generation_result",
    "AUDIO": "audio_result",
    "SUBTITLES": "subtitle_result",
    "EDITING": "editing_result",
    "QUALITY_CONTROL": "quality_control_result",
    "COMPLETED": "final_result",
}
FLAGS = {
    "SCRIPT": "script", "SCENES": "storyboard", "GENERATION": "visual_generation",
    "AUDIO": "audio", "SUBTITLES": "subtitles", "EDITING": "editing",
}
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
_INITIAL_ALLOWED = frozenset({
    "job_id", "status", "current_stage", "created_at", "request",
    "priority", "stages", "revision", "cancel_requested", "updated_at",
    "brain_analysis", "idea_result", "script_result", "research_result",
    "source_result", "reference_result", "legacy_imported",
})
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_OPERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EXTERNAL_CAPABILITIES = {
    "GENERATION": frozenset({"text_to_image", "image_to_image", "text_to_video"}),
    "AUDIO": frozenset({"text_to_speech", "text_to_music"}),
    "SUBTITLES": frozenset({"speech_to_text"}),
    "EDITING": frozenset({"video_editing"}),
}
_WORKER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")
_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}\Z")
_SENSITIVE_KEYS = frozenset({"password", "passwd", "secret", "api_key", "apikey", "access_token", "refresh_token", "private_key", "authorization", "credential", "credentials", "client_secret", "auth_token"})


def _validate_no_credentials(value: Any) -> None:
    """Refuse accidental credential persistence in arbitrary request/output maps."""
    todo = [(value, 0)]
    while todo:
        current, depth = todo.pop()
        if depth > 40:
            raise JobStateError("Nested job content exceeds depth limit")
        if isinstance(current, dict):
            for key, nested in current.items():
                if isinstance(key, str) and key.lower().replace("-", "_") in _SENSITIVE_KEYS:
                    raise JobStateError("Credential fields are prohibited in persisted job data")
                if isinstance(nested, (dict, list)):
                    todo.append((nested, depth + 1))
        elif isinstance(current, list):
            todo.extend((item, depth + 1) for item in current if isinstance(item, (dict, list)))



class JobStateError(ValueError):
    """Conflicting, invalid or stale state; no mutation was committed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialized(value: Any, limit: int) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise JobStateError("Job data must be finite JSON") from exc
    if len(result.encode("utf-8")) > limit:
        raise JobStateError("Job payload exceeds safe size limit")
    return result


def stage_receipt_signature(job_id: str, stage: str, output: dict[str, Any], key: bytes) -> str:
    """HMAC-SHA256 authenticity receipt for an external trusted stage producer.

    Keys must be at least 32 random bytes and provisioned out-of-band.
    The signature authenticates a payload, not physical media integrity.
    """
    if (not isinstance(job_id, str) or not isinstance(stage, str) or
            not isinstance(output, dict) or type(key) is not bytes or len(key) < 32):
        raise JobStateError("Invalid receipt signing input")
    _validate_no_credentials(output)
    document = json.dumps({"job_id": job_id, "stage": stage, "output": output},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(document.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise JobStateError("Signed evidence exceeds safe size limit")
    return hmac.new(key, b"master-job-stage-receipt-v1\x00" + document.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def external_reconciliation_signature(job_id: str, stage: str, operation_id: str,
                                      request_sha256: str, outcome: str,
                                      result_ref: str | None, key: bytes) -> str:
    """Domain-separated HMAC for a manually authenticated provider resolution.

    Bind the exact originally submitted request digest, operation, outcome and
    result reference; do not reuse a stage receipt or a signature from another
    provider operation. This attests operator/provider evidence, not media bytes.
    """
    if (not all(isinstance(x, str) and x for x in (job_id, stage, operation_id)) or
            not isinstance(request_sha256, str) or
            not re.fullmatch(r"[0-9a-f]{64}", request_sha256) or
            outcome not in ("SUCCEEDED", "FAILED") or
            (result_ref is not None and (not isinstance(result_ref, str) or
             not _REFERENCE_RE.fullmatch(result_ref))) or
            (outcome == "SUCCEEDED") != (result_ref is not None) or
            type(key) is not bytes or len(key) < 32):
        raise JobStateError("Invalid reconciliation signing input")
    msg = json.dumps({"job_id": job_id, "stage": stage, "operation_id": operation_id,
                      "request_sha256": request_sha256, "outcome": outcome,
                      "result_ref": result_ref}, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, b"master-job-external-reconcile-v1\x00" + msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(isinstance(x, str) and bool(x.strip()) for x in value)
    return False


def _artifact(result: Any, *fields: str) -> bool:
    return isinstance(result, dict) and any(
        isinstance(result.get(k), str) and bool(result[k].strip()) for k in fields
    )


def _completed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == "COMPLETED"


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") in ("FAILED", "REJECTED")


class MasterJobEngine:
    """Orchestrates one finite pipeline and durable queue without provider side effects.

    ``run_job`` may be called by existing orchestrators with an unsaved job dict.
    Worker APIs: ``claim_next`` -> ``process_claim`` or ``release_claim``.
    All worker leases are opaque, never stored or returned through load APIs.
    """

    def __init__(
        self, storage_path: str | os.PathLike[str] = "agents/jobs/master_jobs.sqlite3",
        gateway: Any = None,
        *,
        lease_seconds: int = 120,
        max_concurrent: int = 4,
        max_recoveries: int = 3,
        max_queued: int = 5000,
        receipt_key: bytes | None = None,
        artifact_root: str | os.PathLike[str] | None = None,
        strict_production: bool = False,
        max_external_operations: int = 128,
    ) -> None:
        if (any(type(x) is not int for x in (lease_seconds, max_concurrent, max_recoveries, max_queued))
                or not 1 <= lease_seconds <= 86400 or not 1 <= max_concurrent <= 512
                or not 0 <= max_recoveries <= 100 or not 1 <= max_queued <= 1000000):
            raise ValueError("Invalid worker limits")
        if receipt_key is not None and (type(receipt_key) is not bytes or len(receipt_key) < 32):
            raise ValueError("receipt_key must be at least 32 bytes")
        if type(strict_production) is not bool or (strict_production and
                (receipt_key is None or artifact_root is None)):
            raise ValueError("Strict production requires a receipt key and artifact root")
        if type(max_external_operations) is not int or not 1 <= max_external_operations <= 10000:
            raise ValueError("Invalid external operation limit")
        self.version = "2.3"
        self.status = "READY"
        self._receipt_key = receipt_key
        self.strict_production = strict_production
        self.max_external_operations = max_external_operations
        # Production opt-in: require on-disk SHA-256 evidence for every media stage.
        # Disabled for legacy deployments that use remote asset references.
        self._artifacts = ArtifactVerifier(artifact_root) if artifact_root is not None else None
        self.storage_path = str(storage_path)
        self.gateway = gateway
        self.lease_seconds = lease_seconds
        self.max_concurrent = max_concurrent
        self.max_recoveries = max_recoveries
        self.max_queued = max_queued
        self.stages = list(STAGES)
        self.stage_handlers = {
            "CREATED": self.handle_created, "PLANNING": self.handle_planning,
            "SCRIPT": self.handle_script, "SCENES": self.handle_scenes,
            "GENERATION": self.handle_generation, "AUDIO": self.handle_audio,
            "SUBTITLES": self.handle_subtitles, "EDITING": self.handle_editing,
            "QUALITY_CONTROL": self.handle_quality_control,
            "COMPLETED": self.handle_completed,
        }
        self._init_db()

    def _init_db(self) -> None:
        path = Path(self.storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise JobStateError("Symlink database paths are not allowed")
        # POSIX: create the database inaccessible to other users by default.
        if not path.exists():
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            # One atomic migration keeps v2 deployments restart-safe: a crash
            # cannot leave half-applied schema upgrades and a version mismatch.
            db.execute("BEGIN IMMEDIATE")
            try:
                v = db.execute("PRAGMA user_version").fetchone()[0]
                if v not in (0, 2, 3):
                    raise JobStateError("Unsupported master job database schema")
                db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
                    status TEXT NOT NULL, current_stage TEXT NOT NULL,
                    priority INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, owner TEXT, lease_hash TEXT,
                    lease_until REAL, recoveries INTEGER NOT NULL DEFAULT 0
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,priority,created_at)")
                db.execute("""CREATE TABLE IF NOT EXISTS submission_keys (
                    key_hash TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS job_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    at TEXT NOT NULL, revision INTEGER NOT NULL,
                    event TEXT NOT NULL, status TEXT NOT NULL,
                    current_stage TEXT NOT NULL
                )""")
                db.execute("CREATE INDEX IF NOT EXISTS job_events_job_seq ON job_events(job_id,seq)")
                db.execute("""CREATE TABLE IF NOT EXISTS external_dispatches (
                    job_id TEXT NOT NULL, stage TEXT NOT NULL,
                    operation_id TEXT NOT NULL, capability TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, response_status TEXT,
                    resolution_ref_hash TEXT, resolver_hash TEXT, resolved_at TEXT,
                    PRIMARY KEY(job_id,stage,operation_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                )""")
                cols = {row[1] for row in db.execute("PRAGMA table_info(external_dispatches)")}
                for name in ("resolution_ref_hash", "resolver_hash", "resolved_at"):
                    if name not in cols:
                        db.execute(f"ALTER TABLE external_dispatches ADD COLUMN {name} TEXT")
                db.execute("PRAGMA user_version=3")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.storage_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")

    @staticmethod
    def _request(request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise JobStateError("request must be an object")
        topic = request.get("topic")
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 1024:
            raise JobStateError("request.topic must be a nonempty string of at most 1024 characters")
        for field in ("language", "type"):
            value = request.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise JobStateError("Invalid request field: " + field)
        _validate_no_credentials(request)
        _serialized(request, MAX_OUTPUT_BYTES)
        return request

    def create_job(self, request: dict[str, Any], *, priority: int = 50) -> dict[str, Any]:
        if isinstance(request, dict) and not request.get("topic") and isinstance(request.get("prompt"), str):
            request = {**request, "topic": request["prompt"][:1024]}
        self._request(request)
        if type(priority) is not int or not 0 <= priority <= 100:
            raise JobStateError("priority must be 0..100")
        return {
            "job_id": str(uuid.uuid4()), "status": "CREATED", "current_stage": "CREATED",
            "created_at": _now(), "request": request, "priority": priority,
            "stages": [], "revision": 0, "cancel_requested": False,
        }

    @staticmethod
    def _check_job(job: Any) -> None:
        if not isinstance(job, dict):
            raise JobStateError("Job must be an object")
        try:
            uuid.UUID(job["job_id"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise JobStateError("Invalid job ID") from exc
        MasterJobEngine._request(job.get("request"))
        if job.get("status") not in ("CREATED", "QUEUED", "RUNNING", "WAITING", *TERMINAL):
            raise JobStateError("Invalid lifecycle state")
        if job.get("current_stage") not in STAGES:
            raise JobStateError("Invalid pipeline stage")
        if type(job.get("priority", 50)) is not int or not 0 <= job.get("priority", 50) <= 100:
            raise JobStateError("Invalid priority")
        if type(job.get("revision", 0)) is not int or job.get("revision", 0) < 0:
            raise JobStateError("Invalid revision")
        if job["status"] == "COMPLETED" and not (
            isinstance(job.get("quality_plan"), dict)
            and isinstance(job.get("final_output"), dict)
            and isinstance(job.get("stage_result"), dict)
            and job["quality_plan"].get("final_quality_status") == "PASSED"
            and job["final_output"].get("status") == "READY"
            and job["stage_result"].get("stage") == "COMPLETED"
            and job["stage_result"].get("status") == "READY"
        ):
            raise JobStateError("A completed job must pass the final quality gate")
        _validate_no_credentials(job)
        _serialized(job, MAX_SNAPSHOT_BYTES)

    @staticmethod
    def _row_job(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["snapshot"])

    def _write(self, db: sqlite3.Connection, job: dict[str, Any], *, token: str | None = None,
               allow_reopen: bool = False) -> dict[str, Any]:
        self._check_job(job)
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        new = dict(job)
        if self._receipt_key is not None and row is None and "script_result" in new:
            raise JobStateError("Signed mode requires stage receipts, not preloaded artifacts")
        if row:
            if token is None:
                if row["owner"] is not None:
                    raise JobStateError("Job is owned by an active worker")
                if self._row_job(row).get("revision", 0) != new.get("revision", 0):
                    raise JobStateError("Stale job revision")
                if row["status"] in TERMINAL and not (
                    allow_reopen and row["status"] == "FAILED" and new["status"] == "WAITING"
                ):
                    raise JobStateError("Terminal jobs are immutable")
            elif not self._valid_lease(row, token):
                raise JobStateError("Worker lease has expired or is not owned")
        elif token is not None:
            raise JobStateError("Cannot lease a missing job")
        else:
            if (new["status"] != "CREATED" or new["current_stage"] != "CREATED"
                    or new.get("revision", 0) != 0
                    or any(field not in _INITIAL_ALLOWED for field in new)):
                raise JobStateError("New jobs must contain pre-execution planning only")
            queued = db.execute("SELECT count(*) FROM jobs WHERE status IN ('CREATED','QUEUED')").fetchone()[0]
            if queued >= self.max_queued:
                raise JobStateError("Job queue full")
        new["updated_at"] = _now()
        new["revision"] = (row and self._row_job(row).get("revision", 0) or 0) + 1
        snapshot = _serialized(new, MAX_SNAPSHOT_BYTES)
        if row:
            db.execute("""UPDATE jobs SET snapshot=?,status=?,current_stage=?,priority=?,updated_at=?
                WHERE job_id=?""", (snapshot, new["status"], new["current_stage"],
                    new.get("priority", 50), new["updated_at"], new["job_id"]))
        else:
            db.execute("""INSERT INTO jobs
                (job_id,snapshot,status,current_stage,priority,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (new["job_id"], snapshot, new["status"],
                  new["current_stage"], new.get("priority", 50),
                  new.get("created_at", new["updated_at"]), new["updated_at"]))
        self._event(db, new, "WRITE")
        job.clear()
        job.update(new)
        return job

    @staticmethod
    def _event(db: sqlite3.Connection, job: dict[str, Any], event: str) -> None:
        """Transactionally record metadata only: no request, prompts or credentials."""
        db.execute("""INSERT INTO job_events(job_id,at,revision,event,status,current_stage)
            VALUES(?,?,?,?,?,?)""", (job["job_id"], _now(), job.get("revision", 0),
            event, job["status"], job["current_stage"]))

    def job_events(self, job_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Local trusted operator view. Service layers must enforce authorization."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise JobStateError("Invalid event limit")
        with self._db() as db:
            rows = db.execute("""SELECT seq,at,revision,event,status,current_stage FROM
                job_events WHERE job_id=? ORDER BY seq DESC LIMIT ?""", (job_id, limit)).fetchall()
            return [dict(row) for row in reversed(rows)]

    def submit_request(self, request: dict[str, Any], *, idempotency_key: str,
                       priority: int = 50) -> dict[str, Any]:
        """Atomic, deduplicated submission. Identical retries return the original job."""
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise JobStateError("Invalid idempotency key")
        job = self.create_job(request, priority=priority)
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        # JSON key ordering is not part of the request identity.
        fingerprint = hashlib.sha256(json.dumps({"request": job["request"], "priority": priority},
            ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self._tx() as db:
            row = db.execute("SELECT job_id,request_hash FROM submission_keys WHERE key_hash=?",
                             (key_hash,)).fetchone()
            if row:
                if row["request_hash"] != fingerprint:
                    raise JobStateError("Idempotency key used for a different request")
                record = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
                if not record:
                    raise JobStateError("Idempotency record refers to a missing job")
                return json.loads(record["snapshot"])
            self._write(db, job)
            db.execute("INSERT INTO submission_keys(key_hash,job_id,request_hash,created_at) VALUES(?,?,?,?)",
                       (key_hash, job["job_id"], fingerprint, _now()))
            return job

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Persist pre-execution planning only; never manually advance a live pipeline."""
        self._check_job(job)
        # Only internal pre-execution planning fields may be attached with this
        # compatibility method. All later artifacts require record_stage_output.
        if (job["status"] != "CREATED" or job["current_stage"] != "CREATED"
                or any(key not in _INITIAL_ALLOWED for key in job)):
            raise JobStateError("Only pre-execution planning can be saved directly")
        if self._receipt_key is not None and "script_result" in job:
            raise JobStateError("Signed mode requires stage receipts, not preloaded artifacts")
        with self._tx() as db:
            row = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            if row:
                old = json.loads(row["snapshot"])
                if old["status"] != "CREATED" or old["current_stage"] != "CREATED":
                    raise JobStateError("Stage transition requires a worker lease")
            elif job.get("revision", 0) != 0:
                raise JobStateError("New jobs must have revision zero")
            return self._write(db, job)

    def load_job(self, job_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return json.loads(row["snapshot"]) if row else None

    def load_jobs(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [json.loads(r["snapshot"]) for r in db.execute(
                "SELECT snapshot FROM jobs ORDER BY created_at, job_id")]

    @staticmethod
    def _valid_lease(row: sqlite3.Row, token: str) -> bool:
        return (isinstance(token, str) and row["owner"] is not None
            and row["lease_until"] is not None and row["lease_until"] > time.time()
            and isinstance(row["lease_hash"], str)
            and secrets.compare_digest(row["lease_hash"], hashlib.sha256(token.encode()).hexdigest()))

    def _reap(self, db: sqlite3.Connection) -> None:
        now = time.time()
        for row in db.execute("SELECT * FROM jobs WHERE owner IS NOT NULL AND (lease_until<=? OR lease_until IS NULL)", (now,)):
            job = self._row_job(row)
            if row["status"] in TERMINAL:
                pass
            elif job.get("cancel_requested"):
                job["status"] = "CANCELLED"
            elif row["recoveries"] >= self.max_recoveries:
                job["status"] = "FAILED"
                job["stage_result"] = {"stage": job["current_stage"], "status": "FAILED", "reason": "WORKER_RECOVERY_LIMIT"}
            else:
                job["status"] = "QUEUED"
            job["updated_at"] = _now()
            job["revision"] = job.get("revision", 0) + 1
            db.execute("""UPDATE jobs SET snapshot=?,status=?,owner=NULL,lease_hash=NULL,
                lease_until=NULL,recoveries=recoveries+1,updated_at=? WHERE job_id=?""",
                (_serialized(job, MAX_SNAPSHOT_BYTES), job["status"], job["updated_at"], job["job_id"]))
            self._event(db, job, "LEASE_EXPIRED")

    def _claim(self, db: sqlite3.Connection, worker_id: str, job_id: str | None) -> dict[str, Any] | None:
        self._reap(db)
        active = db.execute("SELECT count(*) FROM jobs WHERE owner IS NOT NULL AND lease_until>?", (time.time(),)).fetchone()[0]
        if active >= self.max_concurrent:
            return None
        if job_id:
            row = db.execute("SELECT * FROM jobs WHERE job_id=? AND status IN ('CREATED','QUEUED') AND owner IS NULL", (job_id,)).fetchone()
        else:
            row = db.execute("""SELECT * FROM jobs WHERE status IN ('CREATED','QUEUED') AND owner IS NULL
                ORDER BY priority DESC,created_at ASC,job_id ASC LIMIT 1""").fetchone()
        if not row:
            return None
        token = secrets.token_urlsafe(32)
        job = self._row_job(row)
        job["status"] = "RUNNING"
        job["updated_at"] = _now()
        job["revision"] = job.get("revision", 0) + 1
        db.execute("""UPDATE jobs SET snapshot=?,status='RUNNING',owner=?,lease_hash=?,lease_until=?,updated_at=?
            WHERE job_id=?""", (_serialized(job, MAX_SNAPSHOT_BYTES), worker_id,
            hashlib.sha256(token.encode()).hexdigest(), time.time() + self.lease_seconds,
            job["updated_at"], job["job_id"]))
        self._event(db, job, "CLAIM")
        return {"job": job, "lease_token": token, "worker_id": worker_id}

    def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not _WORKER_RE.fullmatch(worker_id):
            raise JobStateError("Invalid worker ID")
        with self._tx() as db:
            return self._claim(db, worker_id, None)

    def renew_lease(self, job_id: str, worker_id: str, lease_token: str) -> bool:
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["owner"] != worker_id or not self._valid_lease(row, lease_token):
                return False
            db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time() + self.lease_seconds, job_id))
            return True

    def release_claim(self, job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
        """Return unprocessed claimed job to queue, without marking it completed."""
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["owner"] != worker_id or not self._valid_lease(row, lease_token):
                raise JobStateError("Invalid worker lease")
            job = self._row_job(row)
            job["status"] = "CANCELLED" if job.get("cancel_requested") else "QUEUED"
            self._write(db, job, token=lease_token)
            self._clear_owner(db, job_id)
            return job

    @staticmethod
    def _clear_owner(db: sqlite3.Connection, job_id: str) -> None:
        db.execute("UPDATE jobs SET owner=NULL,lease_hash=NULL,lease_until=NULL WHERE job_id=?", (job_id,))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobStateError("Job not found")
            job = self._row_job(row)
            if job["status"] in TERMINAL:
                return job
            job["cancel_requested"] = True
            if row["owner"] is None:
                job["status"] = "CANCELLED"
            job["updated_at"] = _now()
            job["revision"] = job.get("revision", 0) + 1
            db.execute("UPDATE jobs SET snapshot=?,status=?,updated_at=? WHERE job_id=?", (
                _serialized(job, MAX_SNAPSHOT_BYTES), job["status"], job["updated_at"], job_id))
            self._event(db, job, "CANCEL_REQUEST")
            return job

    def reopen_failed_job(self, job_id: str, *, reason: str) -> dict[str, Any]:
        """Explicit operator recovery. Never automatically re-dispatches charged work."""
        if not isinstance(reason, str) or not 4 <= len(reason.strip()) <= 128:
            raise JobStateError("A short manual recovery reason is required")
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobStateError("Job not found")
            job = self._row_job(row)
            if row["owner"] is not None or job["status"] != "FAILED":
                raise JobStateError("Only inactive failed jobs can be reopened")
            job.setdefault("recovery_history", []).append({
                "stage": job["current_stage"], "at": _now(), "reason": reason.strip(),
            })
            job["status"] = "WAITING"
            return self._write(db, job, allow_reopen=True)

    def record_stage_output(self, job_id: str, stage: str, output: dict[str, Any],
                            *, receipt_signature: str | None = None) -> dict[str, Any]:
        """Stage hook: optionally require signed receipts; authenticate APIs separately."""
        if stage not in RESULT_KEYS or not isinstance(output, dict):
            raise JobStateError("Unknown output stage or invalid output")
        _validate_no_credentials(output)
        _serialized(output, MAX_OUTPUT_BYTES)
        if self._receipt_key is not None:
            expected = stage_receipt_signature(job_id, stage, output, self._receipt_key)
            if (not isinstance(receipt_signature, str) or
                    not re.fullmatch(r"[a-f0-9]{64}", receipt_signature) or
                    not hmac.compare_digest(expected, receipt_signature)):
                raise JobStateError("Invalid or missing signed stage receipt")
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobStateError("Job not found")
            job = self._row_job(row)
            if row["owner"] is not None or job["status"] in TERMINAL:
                raise JobStateError("Job is active or terminal")
            if job["current_stage"] != stage:
                raise JobStateError("Output does not match the current stage")
            field = RESULT_KEYS[stage]
            if job.get(field) == output:
                return job  # Idempotent duplicate notification.
            job[field] = output
            job["status"] = "QUEUED"
            return self._write(db, job)

    def import_legacy_jobs(self, legacy_path: str | os.PathLike[str], *, dry_run: bool = True) -> dict[str, int]:
        """Opt-in migration of old jobs.json. All imported jobs restart unverified.

        Historical ``COMPLETED`` markers are deliberately NOT trusted because the
        previous engine could declare success without a render. Existing rows
        are never overwritten and the original JSON is never modified.
        """
        path = Path(legacy_path)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20 * 1024 * 1024:
            raise JobStateError("Missing, unsafe or oversized legacy state")
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise JobStateError("Malformed legacy job state") from exc
        if not isinstance(legacy, list):
            raise JobStateError("Legacy jobs must be a list")
        counts = {"scanned": len(legacy), "imported": 0, "skipped": 0, "invalid": 0}
        seen: set[str] = set()
        for old in legacy:
            try:
                if not isinstance(old, dict):
                    raise JobStateError("Invalid legacy record")
                job_id = old["job_id"]
                uuid.UUID(job_id)
                if job_id in seen:
                    raise JobStateError("Duplicate legacy job")
                seen.add(job_id)
                new = self.create_job(old["request"])
                new["job_id"] = job_id
                new["legacy_imported"] = True
                # Retain non-executable planning inputs, never prior completion flags.
                for key in ("brain_analysis", "idea_result", "script_result", "research_result"):
                    if isinstance(old.get(key), dict):
                        new[key] = old[key]
                _serialized(new, MAX_SNAPSHOT_BYTES)
            except (JobStateError, KeyError, TypeError, ValueError, OverflowError):
                counts["invalid"] += 1
                continue
            with self._tx() as db:
                exists = db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                if exists:
                    counts["skipped"] += 1
                else:
                    if not dry_run:
                        self._write(db, new)
                    counts["imported"] += 1
        return counts

    def update_stage(self, job: dict[str, Any], stage: str) -> dict[str, Any]:
        if stage not in STAGES:
            raise JobStateError("Unknown stage: " + str(stage))
        job["current_stage"] = stage
        return job

    def advance_stage(self, job: dict[str, Any]) -> dict[str, Any]:
        i = STAGES.index(job["current_stage"])
        plan = job.get("brain_analysis", {}).get("production_plan", {})
        if not isinstance(plan, dict):
            plan = {}
        while i < len(STAGES) - 1:
            i += 1
            candidate = STAGES[i]
            flag = FLAGS.get(candidate)
            if flag is not None and plan.get(flag) is False:
                job.setdefault("stages", []).append({"stage": candidate, "status": "SKIPPED", "at": _now()})
                continue
            job["current_stage"] = candidate
            return job
        return job

    def _result(self, job: dict[str, Any], stage: str, status: str = "READY", **details: Any) -> dict[str, Any]:
        job["stage_result"] = {"stage": stage, "status": status, **details}
        return job

    def handle_created(self, job, stage=None):
        self._request(job.get("request"))
        return self._result(job, "CREATED", request_received=True)

    def handle_planning(self, job, stage=None):
        request = job["request"]
        plan = job.get("brain_analysis", {}).get("production_plan", {})
        if not isinstance(plan, dict) or any(type(v) is not bool for v in plan.values()):
            return self._result(job, "PLANNING", "FAILED", reason="INVALID_PRODUCTION_FLAGS")
        job["production_plan"] = {
            "topic": request["topic"], "language": request.get("language", "auto"),
            "duration": request.get("duration", "auto"),
            "content_type": request.get("type", "cinematic_video"),
            "required": {key: plan.get(key, True) for key in (*FLAGS.values(), "quality_control")},
        }
        return self._result(job, "PLANNING", plan_ready=True)

    def handle_script(self, job, stage=None):
        result = job.get("script_result", {})
        if _failed(result):
            return self._result(job, "SCRIPT", "FAILED", reason="SCRIPT_FAILED")
        if not isinstance(result, dict) or not _has_content(result.get("script")):
            return self._result(job, "SCRIPT", "WAITING", reason="SCRIPT_OUTPUT_REQUIRED")
        job["script_plan"] = {"script": result["script"], "dialogue": result.get("dialogue", [])}
        return self._result(job, "SCRIPT", script_ready=True)

    def handle_scenes(self, job, stage=None):
        result = job.get("scene_result") or job.get("script_result") or {}
        if _failed(result):
            return self._result(job, "SCENES", "FAILED", reason="SCENES_FAILED")
        scenes = result.get("scenes") if isinstance(result, dict) else None
        if not isinstance(scenes, list) or not scenes or any(
            not isinstance(x, dict) or not _has_content(x.get("description", x.get("image_prompt")))
            for x in scenes
        ):
            return self._result(job, "SCENES", "WAITING", reason="SCENES_REQUIRED")
        job["scene_plan"] = {"scenes": scenes}
        return self._result(job, "SCENES", scene_count=len(scenes))

    def _media_gate(self, job: dict[str, Any], stage: str) -> tuple[str, str] | None:
        """Fail closed on corrupted evidence; wait if producer has not supplied it.

        This validates locally owned files, not remote URLs or perceptual media
        quality. Remote media requires a trusted ingestion service to download,
        scan and place files inside artifact_root before receipt submission.
        """
        if self._artifacts is None:
            return None
        output = job.get(RESULT_KEYS[stage], {})
        if not isinstance(output, dict):
            return ("WAITING", "VERIFIED_MEDIA_EVIDENCE_REQUIRED")
        if stage == "GENERATION":
            records = output.get("generated_outputs")
            if not isinstance(records, list) or not records:
                return ("WAITING", "VERIFIED_MEDIA_EVIDENCE_REQUIRED")
            artifacts = [(o.get("result", o) if isinstance(o, dict) else {}) for o in records]
            allowed = frozenset({"image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm"})
        elif stage == "AUDIO":
            artifacts = [output]
            allowed = frozenset({"audio/wav", "audio/mpeg"})
        elif stage == "SUBTITLES":
            artifacts = [output]
            allowed = frozenset({"text/vtt", "text/srt", "text/plain"})
        elif stage == "EDITING":
            artifacts = [output]
            allowed = frozenset({"video/mp4", "video/webm"})
        elif stage == "COMPLETED":
            artifacts = [output]
            allowed = frozenset({"text/plain", "video/mp4", "video/webm", "image/png", "image/jpeg"})
        else:
            raise JobStateError("Unrecognized media verification stage")
        if any(not isinstance(a, dict) or "evidence" not in a for a in artifacts):
            return ("WAITING", "VERIFIED_MEDIA_EVIDENCE_REQUIRED")
        try:
            for artifact in artifacts:
                self._artifacts.verify(artifact["evidence"], allowed_types=allowed)
        except (ArtifactIntegrityError, OSError, ValueError):
            return ("FAILED", "MEDIA_INTEGRITY_FAILED")
        return None

    def handle_generation(self, job, stage=None):
        result = job.get("generation_result", {})
        if _failed(result):
            return self._result(job, "GENERATION", "FAILED", reason="GENERATION_FAILED")
        outputs = result.get("generated_outputs") if isinstance(result, dict) else None
        scenes = job.get("scene_plan", {}).get("scenes", [])
        if isinstance(outputs, list) and any(
            isinstance(o, dict) and _failed(o.get("result", o)) for o in outputs
        ):
            return self._result(job, "GENERATION", "FAILED", reason="GENERATION_ASSET_FAILED")
        if not isinstance(outputs, list) or len(outputs) != len(scenes):
            return self._result(job, "GENERATION", "WAITING", reason="GENERATED_ASSETS_REQUIRED")
        # Require one artifact for each scene, not just a simulated READY flag.
        scene_ids = [str(s.get("scene", i + 1)) for i, s in enumerate(scenes)]
        output_ids = [str(o.get("scene", "")) for o in outputs if isinstance(o, dict)]
        if (len(set(scene_ids)) != len(scene_ids) or output_ids != scene_ids or
            any(not _completed(o.get("result", o)) or not _artifact(
                o.get("result", o), "asset_ref", "output_path", "file_path", "url") for o in outputs)):
            return self._result(job, "GENERATION", "WAITING", reason="INCOMPLETE_GENERATED_ASSETS")
        media_gate = self._media_gate(job, "GENERATION")
        if media_gate:
            return self._result(job, "GENERATION", media_gate[0], reason=media_gate[1])
        job["generation_plan"] = {"generated_outputs": outputs, "generation_status": "COMPLETED"}
        return self._result(job, "GENERATION", generation_ready=True)

    def handle_audio(self, job, stage=None):
        result = job.get("audio_result", {})
        if _failed(result):
            return self._result(job, "AUDIO", "FAILED", reason="AUDIO_FAILED")
        if not _completed(result) or not _artifact(result, "asset_ref", "audio_path", "file_path"):
            return self._result(job, "AUDIO", "WAITING", reason="AUDIO_ASSET_REQUIRED")
        media_gate = self._media_gate(job, "AUDIO")
        if media_gate:
            return self._result(job, "AUDIO", media_gate[0], reason=media_gate[1])
        job["audio_plan"] = {"audio_asset": result}
        return self._result(job, "AUDIO", audio_ready=True)

    def handle_subtitles(self, job, stage=None):
        result = job.get("subtitle_result", {})
        if _failed(result):
            return self._result(job, "SUBTITLES", "FAILED", reason="SUBTITLES_FAILED")
        if not _completed(result) or not _artifact(result, "asset_ref", "subtitle_path", "file_path"):
            return self._result(job, "SUBTITLES", "WAITING", reason="SUBTITLE_ASSET_REQUIRED")
        media_gate = self._media_gate(job, "SUBTITLES")
        if media_gate:
            return self._result(job, "SUBTITLES", media_gate[0], reason=media_gate[1])
        job["subtitle_plan"] = {"subtitle_asset": result}
        return self._result(job, "SUBTITLES", subtitles_ready=True)

    def handle_editing(self, job, stage=None):
        result = job.get("editing_result", {})
        if _failed(result):
            return self._result(job, "EDITING", "FAILED", reason="RENDER_FAILED")
        if not _completed(result) or not _artifact(result, "asset_ref", "render_path", "file_path"):
            return self._result(job, "EDITING", "WAITING", reason="FINAL_RENDER_REQUIRED")
        media_gate = self._media_gate(job, "EDITING")
        if media_gate:
            return self._result(job, "EDITING", media_gate[0], reason=media_gate[1])
        job["editing_plan"] = {"render_asset": result}
        return self._result(job, "EDITING", final_render_ready=True)

    def handle_quality_control(self, job, stage=None):
        result = job.get("quality_control_result", {})
        if self._artifacts is not None:
            # Recheck bytes at the quality gate: files may have been tampered
            # with after the original stage receipt was accepted.
            passed_stages = {x.get("stage") for x in job.get("stages", []) if
                             isinstance(x, dict) and x.get("status") == "PASSED"}
            for prior in ("GENERATION", "AUDIO", "SUBTITLES", "EDITING"):
                if prior in passed_stages and self._media_gate(job, prior):
                    return self._result(job, "QUALITY_CONTROL", "FAILED", reason="PRIOR_MEDIA_INTEGRITY_FAILED")
        if _failed(result) or (isinstance(result, dict) and result.get("status") == "REJECTED"):
            return self._result(job, "QUALITY_CONTROL", "FAILED", reason="QUALITY_REJECTED")
        plan = job.get("brain_analysis", {}).get("production_plan", {})
        if not isinstance(plan, dict):
            plan = {}
        checks = result.get("checks") if isinstance(result, dict) else None
        required = ["integrity"]
        if plan.get("visual_generation", True):
            required.append("visual")
        if plan.get("audio", True):
            required.append("audio")
        if plan.get("subtitles", True):
            required.append("subtitles")
        if plan.get("editing", True):
            required.append("render")
        if plan.get("audio", True) and plan.get("subtitles", True):
            required.append("sync")
        if (not isinstance(result, dict) or result.get("status") != "PASSED" or
            not isinstance(checks, dict) or any(checks.get(k) is not True for k in required)):
            return self._result(job, "QUALITY_CONTROL", "WAITING", reason="VERIFIED_QUALITY_EVIDENCE_REQUIRED")
        job["quality_plan"] = {"final_quality_status": "PASSED", "checks": {k: True for k in required}}
        return self._result(job, "QUALITY_CONTROL", quality_passed=True)

    def handle_completed(self, job, stage=None):
        if job.get("quality_plan", {}).get("final_quality_status") != "PASSED":
            return self._result(job, "COMPLETED", "FAILED", reason="QUALITY_GATE_NOT_PASSED")
        editing = job.get("editing_result") or {}
        supplied = job.get("final_result") or {}
        final_asset = (editing.get("asset_ref") or editing.get("render_path") or
                       editing.get("file_path") or supplied.get("asset_ref"))
        if not isinstance(final_asset, str) or not final_asset.strip():
            return self._result(job, "COMPLETED", "WAITING", reason="FINAL_ARTIFACT_REQUIRED")
        if self._artifacts is not None:
            # A pipeline with editing verifies the render again at final handoff.
            # Other workflows must supply a separate verified final_result.
            proof_stage = "EDITING" if editing else "COMPLETED"
            media_gate = self._media_gate(job, proof_stage)
            if media_gate:
                return self._result(job, "COMPLETED", media_gate[0], reason=media_gate[1])
        job["final_output"] = {"status": "READY", "quality_status": "PASSED", "asset_ref": final_asset,
                               "topic": job["request"]["topic"]}
        return self._result(job, "COMPLETED", final_output=job["final_output"])

    def handle_stage(self, job, stage=None):
        stage = stage or job["current_stage"]
        return self.stage_handlers[stage](job, stage)

    def process_claim(self, job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
        """Advance only while artifacts exist. Releases ownership on all outcomes."""
        try:
            while True:
                with self._tx() as db:
                    row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                    if not row or row["owner"] != worker_id or not self._valid_lease(row, lease_token):
                        raise JobStateError("Stale or missing worker lease")
                    job = self._row_job(row)
                    if job.get("cancel_requested"):
                        job["status"] = "CANCELLED"
                        self._write(db, job, token=lease_token)
                        self._clear_owner(db, job_id)
                        return job
                    stage = job["current_stage"]
                    try:
                        self.stage_handlers[stage](job, stage)
                    except Exception as exc:
                        # Never persist exception text: it may include credentials or user data.
                        self._result(job, stage, "FAILED", reason="HANDLER_ERROR", exception_type=type(exc).__name__)
                    stage_status = job["stage_result"]["status"]
                    if stage_status == "WAITING":
                        job["status"] = "WAITING"
                    elif stage_status == "FAILED":
                        job["status"] = "FAILED"
                    else:
                        job.setdefault("stages", []).append({"stage": stage, "status": "PASSED", "at": _now()})
                        if stage == "COMPLETED":
                            job["status"] = "COMPLETED"
                        else:
                            self.advance_stage(job)
                            job["status"] = "RUNNING"
                    self._write(db, job, token=lease_token)
                    if job["status"] != "RUNNING":
                        self._clear_owner(db, job_id)
                        return job
        except BaseException:
            # Unexpected internal failures must not pretend success. The lease will
            # expire and be safely recovered; no automatic charged call is retried.
            raise

    def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Legacy entry point: persist a new job, then run until blocked or complete."""
        if not isinstance(job, dict):
            raise JobStateError("Job must be an object")
        with self._tx() as db:
            current = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (job.get("job_id"),)).fetchone()
            if not current:
                self._write(db, job)
            elif job.get("revision") != json.loads(current["snapshot"]).get("revision"):
                raise JobStateError("Refusing to run a stale job")
        worker_id = "local-" + uuid.uuid4().hex
        with self._tx() as db:
            claimed = self._claim(db, worker_id, job["job_id"])
        if not claimed:
            stored = self.load_job(job["job_id"])
            if stored is None:
                raise JobStateError("Job not found")
            return stored
        return self.process_claim(job["job_id"], worker_id, claimed["lease_token"])

    def external_dispatch_history(self, job_id: str) -> list[dict[str, Any]]:
        """For trusted operators only; intentionally excludes request bodies and credentials."""
        with self._db() as db:
            return [dict(row) for row in db.execute("""SELECT stage, operation_id, capability,
                request_sha256,state,recorded_at,response_status,
                resolution_ref_hash,resolver_hash,resolved_at FROM external_dispatches
                WHERE job_id=? ORDER BY recorded_at,operation_id""", (job_id,))]

    def reconcile_external_dispatch(self, job_id: str, stage: str, operation_id: str,
                                    outcome: str, *, resolver_id: str,
                                    authorize_reconciliation: bool = False,
                                    result_ref: str | None = None,
                                    receipt_signature: str | None = None) -> dict[str, Any]:
        """Record independently verified provider outcome without moving pipeline stage.

        Caller must authenticate the resolver, verify upstream status and
        billing externally, and ingest/validate media before any stage receipt.
        This API never makes a provider call. Resolution is immutable; exact
        duplicates are idempotent and conflicting callbacks are refused.
        """
        if authorize_reconciliation is not True:
            raise JobStateError("Provider reconciliation requires explicit operator authorization")
        if (not isinstance(resolver_id, str) or not _WORKER_RE.fullmatch(resolver_id)
                or not isinstance(job_id, str) or not isinstance(operation_id, str)
                or not _OPERATION_RE.fullmatch(operation_id)
                or stage not in _EXTERNAL_CAPABILITIES or outcome not in ("SUCCEEDED", "FAILED")
                or (outcome == "SUCCEEDED") != (result_ref is not None)
                or (result_ref is not None and (not isinstance(result_ref, str)
                    or not _REFERENCE_RE.fullmatch(result_ref)))):
            raise JobStateError("Invalid provider reconciliation input")
        actor_hash = hashlib.sha256(resolver_id.encode("utf-8")).hexdigest()
        ref_hash = hashlib.sha256(result_ref.encode("utf-8")).hexdigest() if result_ref else None
        with self._tx() as db:
            row = db.execute("""SELECT request_sha256,state,resolution_ref_hash,
                resolver_hash FROM external_dispatches WHERE job_id=? AND stage=?
                AND operation_id=?""", (job_id, stage, operation_id)).fetchone()
            if row is None:
                raise JobStateError("Cannot reconcile unknown provider operation")
            if self._receipt_key is not None:
                expected = external_reconciliation_signature(job_id, stage, operation_id,
                    row["request_sha256"], outcome, result_ref, self._receipt_key)
                if (not isinstance(receipt_signature, str) or
                        not hmac.compare_digest(expected, receipt_signature)):
                    raise JobStateError("Missing or invalid signed reconciliation evidence")
            desired_state = "CONFIRMED_" + outcome
            if row["state"] in ("CONFIRMED_SUCCEEDED", "CONFIRMED_FAILED"):
                if (row["state"] != desired_state or row["resolution_ref_hash"] != ref_hash):
                    raise JobStateError("Conflicting provider reconciliation is immutable")
                return {"status": "ALREADY_RECONCILED", "outcome": outcome,
                        "job_id": job_id, "stage": stage, "operation_id": operation_id}
            db.execute("""UPDATE external_dispatches SET state=?,resolution_ref_hash=?,
                resolver_hash=?,resolved_at=? WHERE job_id=? AND stage=? AND operation_id=?""",
                (desired_state, ref_hash, actor_hash, _now(), job_id, stage, operation_id))
            # Record the immutable decision separately. Job stage stays WAITING;
            # only independently authenticated stage evidence may advance it.
            jobrow = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if jobrow:
                self._event(db, json.loads(jobrow["snapshot"]), "PROVIDER_RECONCILED")
        return {"status": "RECONCILED", "outcome": outcome,
                "job_id": job_id, "stage": stage, "operation_id": operation_id}

    def dispatch_external_stage(self, job_id: str, stage: str, operation_id: str,
                                capability: str, payload: dict[str, Any], *,
                                authorize_spend: bool = False) -> dict[str, Any]:
        """At-most-once local handoff; never auto-retry unknown provider outcomes.

        Use only from an authenticated service. Each distinct operation ID is a
        consciously approved provider submission (e.g., `scene-1`). If the worker
        crashes after recording an intent, manual provider-side reconciliation
        is necessary. This does not replace upstream idempotency or a budget check.
        """
        if authorize_spend is not True:
            raise JobStateError("External provider spending is disabled by default")
        if (stage not in _EXTERNAL_CAPABILITIES or
                capability not in _EXTERNAL_CAPABILITIES[stage]):
            raise JobStateError("Unsupported stage capability")
        if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
            raise JobStateError("Invalid provider operation ID")
        if not isinstance(payload, dict):
            raise JobStateError("Provider payload must be an object")
        _validate_no_credentials(payload)
        request_hash = hashlib.sha256(_serialized(payload, MAX_OUTPUT_BYTES).encode("utf-8")).hexdigest()
        # Intent commits before the side effect. Even a timeout or crash will
        # leave the unique row and block blind retries of a paid request.
        with self._tx() as db:
            row = db.execute("SELECT status,current_stage,owner FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["status"] != "WAITING" or row["current_stage"] != stage or row["owner"] is not None:
                raise JobStateError("Job is not waiting for this external stage")
            exists = db.execute("SELECT 1 FROM external_dispatches WHERE job_id=? AND stage=? AND operation_id=?",
                                (job_id, stage, operation_id)).fetchone()
            if exists:
                raise JobStateError("Provider operation already submitted; reconcile before further action")
            count = db.execute("SELECT count(*) FROM external_dispatches WHERE job_id=?",
                               (job_id,)).fetchone()[0]
            if count >= self.max_external_operations:
                raise JobStateError("External operation limit reached; reconcile spending")
            db.execute("""INSERT INTO external_dispatches
                (job_id,stage,operation_id,capability,request_sha256,state,recorded_at)
                VALUES(?,?,?,?,?,'UNKNOWN',?)""", (job_id, stage, operation_id, capability, request_hash, _now()))
        safe_status = "UNKNOWN"
        try:
            if self.gateway is None:
                from agents.execution.execution_gateway import ExecutionGateway
                self.gateway = ExecutionGateway()
            response = self.gateway.execute(capability, payload)
            if isinstance(response, dict) and response.get("status") in ("COMPLETED", "FAILED", "QUEUED", "SUBMITTED", "RUNNING", "ROUTABLE"):
                safe_status = response["status"]
        except Exception:
            # Never log exception messages: upstream errors can contain secrets.
            pass
        try:
            with self._tx() as db:
                db.execute("""UPDATE external_dispatches SET state='RECONCILE',response_status=?
                    WHERE job_id=? AND stage=? AND operation_id=? AND state='UNKNOWN'""",
                    (safe_status, job_id, stage, operation_id))
        except sqlite3.Error:
            # Intent still exists in durable store; caller must reconcile.
            pass
        return {"status": "RECONCILIATION_REQUIRED", "provider_status": safe_status,
                "job_id": job_id, "stage": stage, "operation_id": operation_id}

    def execute_capability(self, capability: str, payload: dict[str, Any] | None = None) -> Any:
        """Legacy entry point disabled: use durable dispatch_external_stage."""
        raise JobStateError("Use an explicitly authorized, durable stage dispatch")

    def status_info(self) -> dict[str, str]:
        return {"engine": "Master Job Engine", "version": self.version, "status": self.status}


if __name__ == "__main__":
    print(MasterJobEngine().status_info())
