"""Production-oriented provider reconciliation / schema migration regression tests.

No live API calls, secrets, or chargeable providers are used.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agents.jobs.job_engine import (
    JobStateError, MasterJobEngine, external_reconciliation_signature,
    stage_receipt_signature,
)


class MockGateway:
    def __init__(self):
        self.calls = 0

    def execute(self, capability, payload):
        self.calls += 1
        return {"status": "QUEUED", "request_id": "test-id"}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "jobs.sqlite3"
        self.gateway = MockGateway()
        self.engine = MasterJobEngine(self.path, gateway=self.gateway)
        self.job = self._ready(self.engine)

    @staticmethod
    def _ready(engine):
        job = engine.create_job({"topic": "offline reconciliation"})
        engine.save_job(job)
        engine.run_job(job)
        engine.record_stage_output(job["job_id"], "SCRIPT", {
            "script": ["Test narration"],
            "scenes": [{"scene": 1, "description": "One synthetic scene"}],
        })
        state = engine.run_job(engine.load_job(job["job_id"]))
        assert (state["status"], state["current_stage"]) == ("WAITING", "GENERATION")
        return job

    def dispatch(self, operation="scene-1"):
        return self.engine.dispatch_external_stage(self.job["job_id"], "GENERATION", operation,
            "text_to_image", {"prompt": "private customer script"}, authorize_spend=True)

    def test_confirmation_is_explicit_immutable_and_does_not_advance_media_stage(self):
        self.dispatch()
        job_id = self.job["job_id"]
        with self.assertRaisesRegex(JobStateError, "authorization"):
            self.engine.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
                "SUCCEEDED", resolver_id="operator", result_ref="asset:verified")
        confirmed = self.engine.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
            "SUCCEEDED", resolver_id="operator", result_ref="asset:verified",
            authorize_reconciliation=True)
        self.assertEqual(confirmed["status"], "RECONCILED")
        self.assertEqual(self.engine.load_job(job_id)["status"], "WAITING")
        duplicate = self.engine.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
            "SUCCEEDED", resolver_id="operator", result_ref="asset:verified",
            authorize_reconciliation=True)
        self.assertEqual(duplicate["status"], "ALREADY_RECONCILED")
        for outcome, ref in (("FAILED", None), ("SUCCEEDED", "asset:other")):
            with self.subTest(outcome=outcome, ref=ref), self.assertRaisesRegex(JobStateError, "immutable"):
                self.engine.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
                    outcome, resolver_id="operator", result_ref=ref,
                    authorize_reconciliation=True)
        history = self.engine.external_dispatch_history(job_id)
        self.assertEqual(history[0]["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(history[0]["resolution_ref_hash"],
                         hashlib.sha256(b"asset:verified").hexdigest())
        self.assertNotIn("private customer script", json.dumps(history))
        self.assertNotIn("asset:verified", json.dumps(history))
        self.assertNotIn("operator", json.dumps(history))
        self.assertEqual(self.gateway.calls, 1)

    def test_failed_outcome_is_audit_only_and_never_forges_job_success_or_retry(self):
        self.dispatch()
        result = self.engine.reconcile_external_dispatch(self.job["job_id"], "GENERATION",
            "scene-1", "FAILED", resolver_id="ops-1", authorize_reconciliation=True)
        self.assertEqual(result["outcome"], "FAILED")
        self.assertEqual(self.engine.load_job(self.job["job_id"])["status"], "WAITING")
        with self.assertRaisesRegex(JobStateError, "already submitted"):
            self.dispatch()
        self.assertEqual(self.gateway.calls, 1)

    def test_unknown_operation_and_invalid_data_never_change_state(self):
        with self.assertRaisesRegex(JobStateError, "unknown"):
            self.engine.reconcile_external_dispatch(self.job["job_id"], "GENERATION",
                "not-exists", "FAILED", resolver_id="operator", authorize_reconciliation=True)
        self.dispatch()
        for ref in ("https://provider.test?a=b", " bad ", "", "\nasset:abc"):
            with self.subTest(ref=ref), self.assertRaises(JobStateError):
                self.engine.reconcile_external_dispatch(self.job["job_id"], "GENERATION",
                    "scene-1", "SUCCEEDED", resolver_id="operator",
                    result_ref=ref, authorize_reconciliation=True)
        self.assertEqual(self.engine.external_dispatch_history(self.job["job_id"])[0]["state"], "RECONCILE")

    def test_external_operation_limit_guards_accidental_fanout(self):
        limited = MasterJobEngine(self.path, gateway=self.gateway, max_external_operations=1)
        limited.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
            "text_to_image", {"prompt": "first"}, authorize_spend=True)
        with self.assertRaisesRegex(JobStateError, "limit reached"):
            limited.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-2",
                "text_to_image", {"prompt": "second"}, authorize_spend=True)
        self.assertEqual(self.gateway.calls, 1)
        self.assertEqual(len(limited.external_dispatch_history(self.job["job_id"])), 1)

    def test_receipts_bind_original_request_operation_outcome_and_asset(self):
        key = b"R" * 32
        signed = MasterJobEngine(self.path, receipt_key=key, gateway=self.gateway)
        job_id = self.job["job_id"]
        self.dispatch()
        request_hash = signed.external_dispatch_history(job_id)[0]["request_sha256"]
        sig = external_reconciliation_signature(job_id, "GENERATION", "scene-1",
            request_hash, "SUCCEEDED", "asset:result", key)
        for supplied in (None, "00" * 32,
                         external_reconciliation_signature(job_id, "GENERATION", "other",
                             request_hash, "SUCCEEDED", "asset:result", key)):
            with self.subTest(sig=supplied), self.assertRaisesRegex(JobStateError, "signed reconciliation"):
                signed.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
                    "SUCCEEDED", resolver_id="operator", result_ref="asset:result",
                    authorize_reconciliation=True, receipt_signature=supplied)
        ok = signed.reconcile_external_dispatch(job_id, "GENERATION", "scene-1",
            "SUCCEEDED", resolver_id="operator", result_ref="asset:result",
            authorize_reconciliation=True, receipt_signature=sig)
        self.assertEqual(ok["status"], "RECONCILED")
        self.assertEqual(self.gateway.calls, 1)

    def test_provider_return_cannot_overwrite_reconciliation_committed_mid_call(self):
        job_id = self.job["job_id"]
        engine = self.engine
        class RacingGateway:
            calls = 0
            def execute(self, capability, payload):
                self.calls += 1
                # A webhook may arrive while dispatch_external_stage is still
                # waiting for the provider HTTP response.
                engine.reconcile_external_dispatch(job_id, "GENERATION", "scene-race",
                    "SUCCEEDED", resolver_id="webhook", result_ref="asset:confirmed",
                    authorize_reconciliation=True)
                return {"status": "QUEUED"}
        race = RacingGateway()
        engine.gateway = race
        dispatched = engine.dispatch_external_stage(job_id, "GENERATION", "scene-race",
            "text_to_image", {"prompt": "image"}, authorize_spend=True)
        self.assertEqual(dispatched["status"], "RECONCILIATION_REQUIRED")
        history = engine.external_dispatch_history(job_id)
        self.assertEqual(history[0]["state"], "CONFIRMED_SUCCEEDED")
        self.assertEqual(race.calls, 1)

    def test_concurrent_reconciliation_is_consistent_with_across_process_connections(self):
        self.dispatch()
        replies, errors = [], []
        barrier = threading.Barrier(5)

        def resolve():
            try:
                engine = MasterJobEngine(self.path)
                barrier.wait(timeout=10)
                replies.append(engine.reconcile_external_dispatch(self.job["job_id"],
                    "GENERATION", "scene-1", "FAILED", resolver_id="ops",
                    authorize_reconciliation=True)["status"])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=resolve) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(errors)
        self.assertEqual(replies.count("RECONCILED"), 1)
        self.assertEqual(replies.count("ALREADY_RECONCILED"), 4)
        self.assertEqual(self.gateway.calls, 1)

    def test_old_v2_database_schema_migrates_atomically_without_losing_jobs(self):
        old_id = self.job["job_id"]
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("ALTER TABLE external_dispatches DROP COLUMN resolution_ref_hash")
            db.execute("ALTER TABLE external_dispatches DROP COLUMN resolver_hash")
            db.execute("ALTER TABLE external_dispatches DROP COLUMN resolved_at")
            db.execute("PRAGMA user_version=2")
        upgraded = MasterJobEngine(self.path)
        self.assertEqual(upgraded.load_job(old_id)["current_stage"], "GENERATION")
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row[1] for row in db.execute("PRAGMA table_info(external_dispatches)")}
            self.assertTrue({"resolution_ref_hash", "resolver_hash", "resolved_at"} <= columns)
        again = MasterJobEngine(self.path)
        self.assertIsNotNone(again.load_job(old_id))

    def test_cli_reconciliation_reports_safe_metadata_only(self):
        self.dispatch()
        job_id = self.job["job_id"]

        def cli(*args, status=0):
            process = subprocess.run([sys.executable, "-m", "agents.jobs.job_cli",
                "--db", str(self.path), *args], capture_output=True, text=True)
            self.assertEqual(process.returncode, status)
            return process

        unsigned = cli("reconcile", job_id, "GENERATION", "scene-1", "SUCCEEDED",
            "--resolver-id", "operator", "--result-ref", "asset:result", status=2)
        self.assertIn("JobStateError", unsigned.stderr)
        verified = cli("reconcile", job_id, "GENERATION", "scene-1", "SUCCEEDED",
            "--resolver-id", "operator", "--result-ref", "asset:result", "--authorize")
        self.assertEqual(json.loads(verified.stdout)["status"], "RECONCILED")
        visible = cli("dispatch-history", job_id)
        self.assertIn("CONFIRMED_SUCCEEDED", visible.stdout)
        for private in ("private customer script", "operator", "asset:result"):
            self.assertNotIn(private, visible.stdout)
        self.assertEqual(self.gateway.calls, 1)

    def test_strict_production_is_opt_in_and_fail_closed_without_proofs(self):
        directory = self.path.parent / "media"
        directory.mkdir()
        key = b"S" * 32
        for options in ({}, {"receipt_key": key}, {"artifact_root": directory}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "Strict production"):
                MasterJobEngine(self.path, strict_production=True, **options)
        strict = MasterJobEngine(self.path, strict_production=True,
                                 receipt_key=key, artifact_root=directory)
        self.assertTrue(strict.strict_production)
        another = strict.create_job({"topic": "secure"})
        another["script_result"] = {"script": ["unsafe preloaded content"]}
        with self.assertRaises(JobStateError):
            strict.save_job(another)
        strict_job = strict.create_job({"topic": "auth workflow"})
        strict.save_job(strict_job)
        strict.run_job(strict_job)
        with self.assertRaisesRegex(JobStateError, "signed stage receipt"):
            strict.record_stage_output(strict_job["job_id"], "SCRIPT", {"script": ["real"]})
        output = {"script": ["real"], "scenes": [{"scene": 1, "description": "s"}]}
        signature = stage_receipt_signature(strict_job["job_id"], "SCRIPT", output, key)
        strict.record_stage_output(strict_job["job_id"], "SCRIPT", output, receipt_signature=signature)
        self.assertEqual(strict.run_job(strict.load_job(strict_job["job_id"]))["current_stage"], "GENERATION")


if __name__ == "__main__":
    unittest.main()
