"""Loopback-only WSGI/HTTP smoke test: local sockets, no cloud calls.

Validates signed request boundaries, tenant isolation and stage evidence over
an actual HTTP connection. This is not TLS/deployed infrastructure verification.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

from agents.jobs.job_engine import stage_receipt_signature
from agents.jobs.tenant_gateway import SignedTenantJobApp, TenantSecrets, request_signature


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        tenants = {
            "alpha": TenantSecrets(secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(32)),
            "beta": TenantSecrets(secrets.token_bytes(32), secrets.token_bytes(32), secrets.token_bytes(32)),
        }
        app = SignedTenantJobApp(Path(root) / "state", tenants, allow_insecure_loopback=True)
        server = make_server("127.0.0.1", 0, app, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            def send(tenant: str, role: str, method: str, path: str, payload=None, *, nonce=None):
                body = json.dumps(payload, separators=(",", ":")).encode() if method == "POST" else b""
                timestamp = str(int(time.time()))
                nonce = nonce or secrets.token_urlsafe(24)
                key = tenants[tenant].client_key if role == "client" else tenants[tenant].producer_key
                signature = request_signature(method, path, tenant, role, timestamp, nonce, body, key)
                req = urllib.request.Request(base + path, data=body if method == "POST" else None,
                    method=method, headers={"Content-Type": "application/json", "X-AI-Tenant": tenant,
                    "X-AI-Role": role, "X-AI-Timestamp": timestamp, "X-AI-Nonce": nonce,
                    "X-AI-Signature": signature})
                try:
                    with urllib.request.urlopen(req, timeout=10) as result:
                        return result.status, json.load(result)
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read().decode())

            code, record = send("alpha", "client", "POST", "/v1/jobs", {
                "request": {"topic": "sensitive project prompt"}, "idempotency_key": "offline-one"})
            assert code == 200, (code, record)
            job_id = record["job_id"]
            assert "sensitive" not in json.dumps(record)
            assert send("beta", "client", "GET", "/v1/jobs/" + job_id)[0] == 404
            assert send("alpha", "client", "GET", "/v1/jobs/" + job_id)[0] == 200
            engine = app._engine("alpha")
            state = engine.run_job(engine.load_job(job_id))
            assert state["current_stage"] == "SCRIPT" and state["status"] == "WAITING"
            result = {"script": ["real script"], "scenes": [{"scene": 1, "description": "one"}]}
            sig = stage_receipt_signature(job_id, "SCRIPT", result, tenants["alpha"].receipt_key)
            path = f"/v1/jobs/{job_id}/stages/SCRIPT"
            code, output = send("alpha", "producer", "POST", path, {
                "output": result, "receipt_signature": sig})
            assert code == 200 and output["status"] == "QUEUED", (code, output)
            assert send("beta", "producer", "POST", path, {
                "output": result, "receipt_signature": sig})[0] == 404
            assert send("alpha", "client", "POST", path, {
                "output": result, "receipt_signature": sig})[0] == 403
            second_code, same = send("alpha", "client", "POST", "/v1/jobs", {
                "request": {"topic": "sensitive project prompt"}, "idempotency_key": "offline-one"})
            assert second_code == 200 and same["job_id"] == job_id
            print("PASS: loopback HTTP HMAC, tenant isolation, producer receipts and idempotency")
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()


if __name__ == "__main__":
    main()
