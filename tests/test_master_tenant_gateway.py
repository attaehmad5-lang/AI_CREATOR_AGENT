"""Offline multi-tenant HTTP security regression: no live provider or internet."""
from __future__ import annotations

import io
import json
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agents.jobs.job_engine import MasterJobEngine, stage_receipt_signature
from agents.jobs.tenant_gateway import (SignedTenantJobApp, TenantSecrets,
                                            request_signature)


class TenantGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1794430000
        self.keys = {
            "acme": TenantSecrets(b"A" * 32, b"B" * 32, b"C" * 32),
            "beta": TenantSecrets(b"D" * 32, b"E" * 32, b"F" * 32),
        }
        self.root = Path(self.tmp.name) / "dbroot"
        self.app = SignedTenantJobApp(self.root, self.keys, clock=lambda: self.now)
        self.counter = 0

    def invoke(self, method="GET", path="/v1/jobs", tenant="acme", role="client",
               payload=None, *, signature=None, timestamp=None, nonce=None, key=None,
               ssl=True, host="127.0.0.1", raw=None, content_type="application/json",
               length=None, app=None):
        self.counter += 1
        if raw is None:
            body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8") if method == "POST" else b""
        else:
            body = raw
        nonce = nonce or ("N" + str(self.counter).zfill(20))
        timestamp = timestamp or str(self.now)
        key = key or (self.keys.get(tenant, self.keys["acme"]).client_key if role == "client" else self.keys.get(tenant, self.keys["acme"]).producer_key)
        signed = signature if signature is not None else request_signature(method, path, tenant,
                                                                          role, timestamp, nonce, body, key)
        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)) if length is None else length,
            "wsgi.input": io.BytesIO(body),
            "wsgi.url_scheme": "https" if ssl else "http",
            "REMOTE_ADDR": host,
            "HTTP_X_AI_TENANT": tenant,
            "HTTP_X_AI_ROLE": role,
            "HTTP_X_AI_TIMESTAMP": timestamp,
            "HTTP_X_AI_NONCE": nonce,
            "HTTP_X_AI_SIGNATURE": signed,
        }
        collector = {}
        def start(status, headers):
            collector["status"] = status
            collector["headers"] = dict(headers)
        content = b"".join((app or self.app)(env, start))
        return int(collector["status"].split()[0]), json.loads(content), collector["headers"]

    def submit(self, tenant="acme", token="req-1"):
        code, response, _ = self.invoke("POST", "/v1/jobs", tenant, payload={
            "request": {"topic": "Private prompt for " + tenant},
            "idempotency_key": token,
        })
        self.assertEqual(code, 200)
        return response["job_id"]

    def test_valid_job_creation_and_redacted_status(self):
        job_id = self.submit()
        code, response, headers = self.invoke(path="/v1/jobs/" + job_id)
        self.assertEqual(code, 200)
        self.assertEqual(response["status"], "CREATED")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Private prompt", json.dumps(response))
        code, response, _ = self.invoke(path=f"/v1/jobs/{job_id}/events")
        self.assertEqual(code, 200)
        self.assertTrue(response["events"])
        self.assertNotIn("Private prompt", json.dumps(response))

    def test_two_tenants_cannot_access_or_cancel_one_another_jobs(self):
        job_id = self.submit()
        for method, suffix, payload in [("GET", "", None), ("GET", "/events", None),
                                        ("POST", "/cancel", {})]:
            status, body, _ = self.invoke(method, f"/v1/jobs/{job_id}{suffix}", "beta", payload=payload)
            self.assertEqual(status, 404)
            self.assertEqual(body, {"error": "NOT_FOUND"})
        status, _, _ = self.invoke(path="/v1/jobs/" + job_id)
        self.assertEqual(status, 200)
        self.assertEqual(self.submit("beta", "req-1") != job_id, True)

    def test_cross_tenant_key_cannot_sign_request(self):
        status, _, _ = self.invoke("POST", "/v1/jobs", payload={"request": {"topic": "x"},
                                      "idempotency_key": "a"}, key=self.keys["beta"].client_key)
        self.assertEqual(status, 401)

    def test_body_or_path_or_role_tampering_rejected(self):
        body = b'{"request":{"topic":"safe"},"idempotency_key":"abc"}'
        nonce = "testnonce1234567890"
        signature = request_signature("POST", "/v1/jobs", "acme", "client",
                                      str(self.now), nonce, body, self.keys["acme"].client_key)
        modified = body.replace(b"safe", b"evil")
        status, _, _ = self.invoke("POST", "/v1/jobs", raw=modified, nonce=nonce, signature=signature)
        self.assertEqual(status, 401)
        status, _, _ = self.invoke("POST", "/v1/jobs/evil", raw=body, nonce=nonce, signature=signature)
        self.assertEqual(status, 401)
        status, _, _ = self.invoke("POST", "/v1/jobs", role="producer", raw=body,
                                   nonce=nonce, signature=signature)
        self.assertEqual(status, 401)

    def test_replay_survives_app_restart(self):
        nonce = "testnonce1234567890"
        req = {"request": {"topic": "hello"}, "idempotency_key": "a1"}
        status, created, _ = self.invoke("POST", "/v1/jobs", payload=req, nonce=nonce)
        self.assertEqual(status, 200)
        app2 = SignedTenantJobApp(self.root, self.keys, clock=lambda: self.now)
        status, body, _ = self.invoke("POST", "/v1/jobs", payload=req, nonce=nonce, app=app2)
        self.assertEqual((status, body["error"]), (409, "REPLAY_REJECTED"))
        self.assertEqual(len(MasterJobEngine(self.root / "tenants/acme/jobs.sqlite3").load_jobs()), 1)
        status, same, _ = self.invoke("POST", "/v1/jobs", payload=req)
        self.assertEqual(status, 200)
        self.assertEqual(created["job_id"], same["job_id"])

    def test_parallel_same_nonce_has_exactly_one_winner(self):
        nonce = "sameNonce1234567890"
        req = {"request": {"topic": "race"}, "idempotency_key": "race"}
        barrier = threading.Barrier(8)
        def attempt(index):
            barrier.wait()
            return self.invoke("POST", "/v1/jobs", payload=req, nonce=nonce)[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(attempt, range(8)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 7)

    def test_expired_future_timestamp_and_missing_tls(self):
        job = self.submit()
        for ts in (str(self.now - 301), str(self.now + 301)):
            self.assertEqual(self.invoke(path="/v1/jobs/" + job, timestamp=ts)[0], 401)
        self.assertEqual(self.invoke(path="/v1/jobs/" + job, ssl=False)[0], 401)
        app = SignedTenantJobApp(self.root, self.keys, clock=lambda: self.now,
                                 allow_insecure_loopback=True)
        self.assertEqual(self.invoke(path="/v1/jobs/" + job, ssl=False, app=app)[0], 200)
        self.assertEqual(self.invoke(path="/v1/jobs/" + job, ssl=False,
                                     host="192.0.2.100", app=app)[0], 401)

    def test_role_boundaries_for_private_producer_stage(self):
        job_id = self.submit()
        output = {"script": ["valid"], "scenes": [{"scene": 1, "description": "demo"}]}
        # Worker advances job to waiting for SCRIPT using its dedicated trusted account.
        engine = self.app._engine("acme")
        self.assertEqual(engine.run_job(engine.load_job(job_id))["current_stage"], "SCRIPT")
        sig = stage_receipt_signature(job_id, "SCRIPT", output, self.keys["acme"].receipt_key)
        endpoint = f"/v1/jobs/{job_id}/stages/SCRIPT"
        event = {"output": output, "receipt_signature": sig}
        self.assertEqual(self.invoke("POST", endpoint, payload=event, role="client")[0], 403)
        self.assertEqual(self.invoke("POST", endpoint, payload={**event,"receipt_signature":"0"*64},
                                     role="producer")[0], 400)
        code, body, _ = self.invoke("POST", endpoint, payload=event, role="producer")
        self.assertEqual(code, 200)
        self.assertEqual(body["current_stage"], "SCRIPT")
        self.assertEqual(body["status"], "QUEUED")
        # Tenant B producer credentials and signed receipt may never read A's ID.
        self.assertEqual(self.invoke("POST", endpoint, tenant="beta", role="producer",
                                     payload=event)[0], 404)

    def test_input_rejection_content_type_size_duplicates_noncanonical(self):
        bad_requests = [
            ("POST", "/v1/jobs", {"request": {"topic": "ok"}, "idempotency_key": "x"}, b'{}{}', "application/json", None, 400),
            ("POST", "/v1/jobs", None, b'{"request":1,"request":2}', "application/json", None, 400),
            ("POST", "/v1/jobs", None, b'{"a":NaN}', "application/json", None, 400),
            ("POST", "/v1/jobs", None, b'{}', "text/plain", None, 415),
            ("POST", "/v1/jobs", None, b'{}', "application/json", "notanumber", 400),
            ("POST", "/v1/jobs", None, b'{}', "application/json", str(256 * 1024 + 1), 413),
        ]
        for method, path, payload, raw, mime, length, expected in bad_requests:
            with self.subTest(raw=raw):
                self.assertEqual(self.invoke(method, path, payload=payload, raw=raw,
                                              content_type=mime, length=length)[0], expected)
        self.assertEqual(self.invoke(path="//v1/jobs")[0], 401)
        self.assertEqual(self.invoke(path="/v1/%6aobs")[0], 401)
        self.assertEqual(self.invoke(path="/v1/jobs/nope")[0], 404)

    def test_cancel_does_not_expose_original_request(self):
        job_id = self.submit()
        status, body, _ = self.invoke("POST", f"/v1/jobs/{job_id}/cancel", payload={})
        self.assertEqual((status, body["status"]), (200, "CANCELLED"))
        self.assertNotIn("Private prompt", json.dumps(body))
        status, body, _ = self.invoke(path=f"/v1/jobs/{job_id}")
        self.assertEqual((status, body["status"]), (200, "CANCELLED"))

    def test_unrecognized_tenant_and_wrong_production_config(self):
        with self.assertRaises(ValueError):
            SignedTenantJobApp(self.root, {"../bad": self.keys["acme"]})
        with self.assertRaises(ValueError):
            TenantSecrets(b"a" * 32, b"a" * 32, b"b" * 32)
        with self.assertRaises(ValueError):
            SignedTenantJobApp(self.root, {"acme": self.keys["acme"],
                "beta": TenantSecrets(self.keys["acme"].client_key, b"Z" * 32, b"Q" * 32)})
        self.assertEqual(self.invoke(tenant="unknown", signature="0"*64)[0], 401)

    def test_database_keys_and_nonces_are_not_written_in_plaintext(self):
        self.submit()
        data = (self.root / "auth_replays.sqlite3").read_bytes()
        self.assertNotIn(b"NN", data)
        self.assertNotIn(self.keys["acme"].client_key, data)
        self.assertNotIn(self.keys["acme"].producer_key, data)
        self.assertTrue((self.root / "tenants/acme/jobs.sqlite3").is_file())
        self.assertFalse((self.root / "tenants/beta/jobs.sqlite3").exists())

    def test_tenant_folder_symlink_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        tenantroot = self.root / "tenants"
        tenantroot.mkdir()
        try:
            (tenantroot / "acme").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink unavailable")
        status, _, _ = self.invoke("POST", "/v1/jobs", payload={
            "request": {"topic": "x"}, "idempotency_key": "s"})
        self.assertEqual(status, 400)
        self.assertEqual(list(outside.iterdir()), [])


    def test_reserved_query_string_is_rejected_before_dispatch(self):
        job_id = self.submit()
        self.counter += 1
        nonce = "QUERY" + str(self.counter).zfill(18)
        path = "/v1/jobs/" + job_id
        signature = request_signature("GET", path, "acme", "client", str(self.now), nonce,
                                      b"", self.keys["acme"].client_key)
        env = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": "stage=COMPLETED",
               "CONTENT_LENGTH": "0", "wsgi.url_scheme": "https", "REMOTE_ADDR": "127.0.0.1",
               "wsgi.input": io.BytesIO(b""), "HTTP_X_AI_TENANT": "acme", "HTTP_X_AI_ROLE": "client",
               "HTTP_X_AI_TIMESTAMP": str(self.now), "HTTP_X_AI_NONCE": nonce,
               "HTTP_X_AI_SIGNATURE": signature}
        responses = []
        result = b"".join(self.app(env, lambda status, hdr: responses.append(status)))
        self.assertEqual(responses[0].split()[0], "401")
        self.assertNotIn(b"Private prompt", result)

    def test_nonce_is_scoped_to_authenticated_tenant(self):
        nonce = "sharedNonce1234567890"
        for tenant in ("acme", "beta"):
            code, _, _ = self.invoke("POST", "/v1/jobs", tenant=tenant, nonce=nonce,
                payload={"request":{"topic":"x"}, "idempotency_key":"same"})
            self.assertEqual(code, 200)

    def test_mutation_methods_and_bogus_id_denied(self):
        job_id = self.submit()
        code, _, _ = self.invoke("POST", "/v1/jobs/" + job_id, payload={})
        self.assertEqual(code, 404)
        self.assertEqual(self.invoke("GET", "/v1/jobs")[0], 404)
        self.assertEqual(self.invoke("POST", "/v1/jobs/" + job_id + "/events", payload={})[0], 404)

    def test_replay_db_symlink_refused_before_auth(self):
        other = Path(self.tmp.name) / "other.sqlite3"
        other.write_text("do not touch")
        root = Path(self.tmp.name) / "fake"
        root.mkdir()
        try:
            (root / "auth_replays.sqlite3").symlink_to(other)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink not available")
        with self.assertRaises(ValueError):
            SignedTenantJobApp(root, self.keys)
        self.assertEqual(other.read_text(), "do not touch")

    def test_tenants_parent_symlink_refused(self):
        outside = Path(self.tmp.name) / "parent-outside"
        outside.mkdir()
        try:
            (self.root / "tenants").symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink not available")
        status, _, _ = self.invoke("POST", "/v1/jobs", payload={
            "request": {"topic": "x"}, "idempotency_key": "s"})
        self.assertEqual(status, 400)
        self.assertEqual(list(outside.iterdir()), [])


    def test_future_dated_nonce_cannot_replay_after_cleanup(self):
        # A valid request timestamp up to 300s in the future remains valid
        # 301s later; the nonce must outlive the full 600s acceptance span.
        nonce = "future-dated-nonce-00001"
        future_timestamp = str(self.now + 300)
        first, _, _ = self.invoke("GET", "/v1/jobs", timestamp=future_timestamp, nonce=nonce)
        self.assertEqual(first, 404)
        self.now += 301
        replay, _, _ = self.invoke("GET", "/v1/jobs", timestamp=future_timestamp, nonce=nonce)
        self.assertEqual(replay, 409)



if __name__ == "__main__":
    unittest.main()
