"""Signed, tenant-isolated WSGI boundary for MasterJobEngine.

Run ONLY behind a trusted TLS-terminating reverse proxy or HTTPS WSGI server.
Never derive the authenticated tenant/role from an unsigned request field.
Provider callbacks need a separate stage receipt signed by the trusted producer:
an API HMAC alone is insufficient to assert that an output was generated.

This module makes no external provider calls and never authorizes spending.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .job_engine import JobStateError, MasterJobEngine, STAGES

_TENANT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}\Z")
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,80}\Z")
_TIMESTAMP = re.compile(r"[0-9]{10,12}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_STAGE_PATH = re.compile(r"/v1/jobs/([0-9a-f-]{36})/stages/([A-Z_]+)\Z")
_JOB_PATH = re.compile(r"/v1/jobs/([0-9a-f-]{36})(?:/(cancel|events))?\Z")
_MAX_BODY = 256 * 1024
_ALLOWED_SKEW = 300


class AuthenticationError(ValueError):
    """Missing/invalid signature, timestamp, nonce, or transport."""


class ReplayError(ValueError):
    """Valid authentication replay; no operation executed twice."""


@dataclass(frozen=True)
class TenantSecrets:
    """Three independent keys; load from a managed secrets store, not a repo."""
    client_key: bytes
    producer_key: bytes
    receipt_key: bytes

    def __post_init__(self) -> None:
        for key in (self.client_key, self.producer_key, self.receipt_key):
            if type(key) is not bytes or len(key) < 32:
                raise ValueError("Every tenant key must contain at least 32 bytes")
        if len({self.client_key, self.producer_key, self.receipt_key}) != 3:
            raise ValueError("Client, producer and receipt keys must differ")


def request_signature(method: str, path: str, tenant: str, role: str,
                      timestamp: str, nonce: str, body: bytes, key: bytes) -> str:
    """HMAC of an exact request; never accept passwords/keys in URLs or payloads."""
    if (method not in ("GET", "POST") or not isinstance(path, str) or
            not isinstance(tenant, str) or not _TENANT.fullmatch(tenant) or
            role not in ("client", "producer") or
            not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp) or
            not isinstance(nonce, str) or not _NONCE.fullmatch(nonce) or
            type(body) is not bytes or len(body) > _MAX_BODY or
            type(key) is not bytes or len(key) < 32):
        raise AuthenticationError("Invalid signed request")
    data = "\n".join((method, path, tenant, role, timestamp, nonce,
                      hashlib.sha256(body).hexdigest())).encode("utf-8")
    return hmac.new(key, b"ai-creator-tenant-http-v1\x00" + data,
                    hashlib.sha256).hexdigest()


def _strict_json(data: bytes) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON keys are prohibited")
            result[key] = value
        return result

    def bad_constant(value: str) -> None:
        raise ValueError("Nonfinite JSON value")

    try:
        result = json.loads(data.decode("utf-8", errors="strict"),
                            object_pairs_hook=no_duplicates, parse_constant=bad_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid JSON object") from exc
    if type(result) is not dict:
        raise ValueError("JSON body must be an object")
    return result


class SignedTenantJobApp:
    """WSGI integration with HMAC auth, durable replay defense, per-tenant stores.

    TLS and client throttling must be configured in the surrounding trusted
    reverse proxy. `allow_insecure_loopback` exists for local tests only.
    The application does not expose paid dispatch, worker credentials, raw
    job payloads, backup files, receipt keys or stage bypasses.
    """

    def __init__(self, state_root: str | os.PathLike[str], tenants: Mapping[str, TenantSecrets],
                 *, clock: Callable[[], int] | None = None,
                 allow_insecure_loopback: bool = False,
                 max_body_bytes: int = _MAX_BODY):
        if (not tenants or not isinstance(tenants, Mapping)
                or type(allow_insecure_loopback) is not bool
                or type(max_body_bytes) is not int or not 1 <= max_body_bytes <= _MAX_BODY):
            raise ValueError("Invalid gateway configuration")
        self.tenants = dict(tenants)
        seen_keys: set[bytes] = set()
        for name, entry in self.tenants.items():
            if type(name) is not str or not _TENANT.fullmatch(name) or not isinstance(entry, TenantSecrets):
                raise ValueError("Invalid tenant configuration")
            for key in (entry.client_key, entry.producer_key, entry.receipt_key):
                if key in seen_keys:
                    raise ValueError("Tenant keys must be globally unique")
                seen_keys.add(key)
        self.state_root = Path(state_root).absolute()
        if self.state_root.is_symlink():
            raise ValueError("State root must not be a symlink")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.state_root.is_dir():
            raise ValueError("State root must be a directory")
        self.clock = clock or (lambda: int(time.time()))
        self.allow_insecure_loopback = allow_insecure_loopback
        self.max_body_bytes = max_body_bytes
        self._nonce_db = self.state_root / "auth_replays.sqlite3"
        if self._nonce_db.is_symlink():
            raise ValueError("Nonce database must not be symlinked")
        with closing(sqlite3.connect(self._nonce_db, timeout=15)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS used_nonces(tenant TEXT NOT NULL,"
                       " nonce_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,"
                       " PRIMARY KEY(tenant,nonce_hash))")
            db.execute("CREATE INDEX IF NOT EXISTS used_nonces_time ON used_nonces(timestamp)")
            db.commit()
        if os.name == "posix":
            os.chmod(self._nonce_db, 0o600)

    def _mark_nonce(self, tenant: str, nonce: str, now: int) -> None:
        digest = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        with closing(sqlite3.connect(self._nonce_db, timeout=15)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                # A future-dated request can remain valid for 2*skew from first
                # presentation. Keep its nonce for that entire lifetime.
                db.execute("DELETE FROM used_nonces WHERE timestamp<?", (now - 2 * _ALLOWED_SKEW,))
                db.execute("INSERT INTO used_nonces(tenant,nonce_hash,timestamp) VALUES(?,?,?)",
                           (tenant, digest, now))
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ReplayError("Request nonce has already been used") from exc
            except BaseException:
                db.rollback()
                raise

    def _engine(self, tenant: str) -> MasterJobEngine:
        tenants_root = self.state_root / "tenants"
        if tenants_root.is_symlink() or (tenants_root.exists() and not tenants_root.is_dir()):
            raise JobStateError("Untrusted tenants directory")
        tenants_root.mkdir(mode=0o700, exist_ok=True)
        folder = tenants_root / tenant
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            raise JobStateError("Untrusted tenant storage directory")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts = folder / "artifacts"
        if artifacts.is_symlink() or (artifacts.exists() and not artifacts.is_dir()):
            raise JobStateError("Untrusted artifact directory")
        artifacts.mkdir(mode=0o700, exist_ok=True)
        return MasterJobEngine(folder / "jobs.sqlite3",
                               receipt_key=self.tenants[tenant].receipt_key,
                               artifact_root=artifacts,
                               strict_production=True)

    @staticmethod
    def _reply(start_response: Callable[..., Any], code: str, payload: dict[str, Any]) -> list[bytes]:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
        start_response(code, [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Length", str(len(data))),
                              ("Cache-Control", "no-store"),
                              ("X-Content-Type-Options", "nosniff")])
        return [data]

    @staticmethod
    def _job_id(value: str) -> str:
        try:
            job_id = str(uuid.UUID(value))
        except (ValueError, TypeError):
            raise ValueError("Invalid job id") from None
        if job_id != value:
            raise ValueError("Noncanonical job id")
        return job_id

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            return self._handle(environ, start_response)
        except AuthenticationError:
            return self._reply(start_response, "401 Unauthorized", {"error": "UNAUTHORIZED"})
        except ReplayError:
            return self._reply(start_response, "409 Conflict", {"error": "REPLAY_REJECTED"})
        except PermissionError:
            return self._reply(start_response, "403 Forbidden", {"error": "FORBIDDEN"})
        except LookupError:
            return self._reply(start_response, "404 Not Found", {"error": "NOT_FOUND"})
        except (JobStateError, ValueError, TypeError):
            return self._reply(start_response, "400 Bad Request", {"error": "INVALID_REQUEST"})
        except Exception:
            # Do not emit exception text, traceback, client body or database error.
            # Service observability belongs in a separate redacted operator sink.
            return self._reply(start_response, "503 Service Unavailable", {"error": "SERVICE_UNAVAILABLE"})

    def _handle(self, env: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        method = env.get("REQUEST_METHOD")
        path = env.get("PATH_INFO")
        tenant = env.get("HTTP_X_AI_TENANT")
        role = env.get("HTTP_X_AI_ROLE")
        timestamp = env.get("HTTP_X_AI_TIMESTAMP")
        nonce = env.get("HTTP_X_AI_NONCE")
        signature = env.get("HTTP_X_AI_SIGNATURE")
        if (env.get("wsgi.url_scheme") != "https" and not (
                self.allow_insecure_loopback and env.get("REMOTE_ADDR") in ("127.0.0.1", "::1"))):
            raise AuthenticationError("Trusted TLS transport required")
        if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant) or tenant not in self.tenants:
            raise AuthenticationError("Invalid tenant")
        if method not in ("GET", "POST") or role not in ("client", "producer"):
            raise AuthenticationError("Invalid method/role")
        if not isinstance(path, str) or len(path) > 180 or path.startswith("//") or "%" in path or "\\" in path or env.get("QUERY_STRING", ""):
            raise AuthenticationError("Noncanonical path")
        if not isinstance(timestamp, str) or not _TIMESTAMP.fullmatch(timestamp) or not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            raise AuthenticationError("Invalid timestamp or nonce")
        if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
            raise AuthenticationError("Invalid signature")
        now = int(self.clock())
        if abs(now - int(timestamp)) > _ALLOWED_SKEW:
            raise AuthenticationError("Expired signed request")
        length = env.get("CONTENT_LENGTH", "")
        if method == "POST":
            if not isinstance(length, str) or not length.isdigit():
                raise ValueError("Content-Length is required")
            size = int(length)
            if size > self.max_body_bytes:
                return self._reply(start_response, "413 Content Too Large", {"error": "PAYLOAD_TOO_LARGE"})
            content_type = env.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return self._reply(start_response, "415 Unsupported Media Type", {"error": "UNSUPPORTED_MEDIA_TYPE"})
            stream = env.get("wsgi.input")
            if not hasattr(stream, "read"):
                raise ValueError("Missing request stream")
            body = stream.read(size)
            if len(body) != size:
                raise ValueError("Truncated request body")
        else:
            if length not in ("", "0", None):
                raise ValueError("GET must not have a body")
            body = b""
        key = self.tenants[tenant].client_key if role == "client" else self.tenants[tenant].producer_key
        expected = request_signature(method, path, tenant, role, timestamp, nonce, body, key)
        if not hmac.compare_digest(expected, signature):
            raise AuthenticationError("Invalid signature")
        # Reject duplicate signed messages across processes and crashes, before any side effect.
        self._mark_nonce(tenant, nonce, now)
        if method == "POST" and path == "/v1/jobs":
            if role != "client":
                raise PermissionError("Only client may submit")
            payload = _strict_json(body)
            if set(payload) - {"request", "idempotency_key", "priority"}:
                raise ValueError("Unsupported request fields")
            engine = self._engine(tenant)
            job = engine.submit_request(payload.get("request"), idempotency_key=payload.get("idempotency_key"),
                                        priority=payload.get("priority", 50))
            return self._reply(start_response, "200 OK", self._status(job))
        match = _JOB_PATH.fullmatch(path)
        if match:
            job_id = self._job_id(match.group(1))
            action = match.group(2)
            if role != "client":
                raise PermissionError("Only client may read/cancel")
            engine = self._engine(tenant)
            job = engine.load_job(job_id)
            if not job:
                raise LookupError("Job not found in tenant")
            if method == "GET" and action is None:
                return self._reply(start_response, "200 OK", self._status(job))
            if method == "GET" and action == "events":
                return self._reply(start_response, "200 OK", {"job_id": job_id,
                    "events": engine.job_events(job_id, limit=50)})
            if method == "POST" and action == "cancel":
                if _strict_json(body):
                    raise ValueError("Cancel body must be empty object")
                return self._reply(start_response, "200 OK", self._status(engine.cancel_job(job_id)))
            raise LookupError("Unknown job route")
        match = _STAGE_PATH.fullmatch(path)
        if match and method == "POST":
            if role != "producer":
                raise PermissionError("Only trusted producer may record")
            job_id = self._job_id(match.group(1))
            stage = match.group(2)
            if stage not in STAGES:
                raise ValueError("Unknown stage")
            payload = _strict_json(body)
            if set(payload) != {"output", "receipt_signature"}:
                raise ValueError("Stage event requires output and receipt signature")
            engine = self._engine(tenant)
            if engine.load_job(job_id) is None:
                raise LookupError("Job not found in tenant")
            return self._reply(start_response, "200 OK", self._status(
                engine.record_stage_output(job_id, stage, payload["output"],
                    receipt_signature=payload["receipt_signature"])))
        raise LookupError("Unknown API route")

    @staticmethod
    def _status(job: dict[str, Any]) -> dict[str, Any]:
        """Do not leak prompts, output content, credentials, or stage receipt metadata."""
        return {"job_id": job["job_id"], "status": job["status"],
                "current_stage": job["current_stage"], "revision": job.get("revision", 0)}
