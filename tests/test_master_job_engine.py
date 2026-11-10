"""Offline regression/contract tests. No network or paid providers are called."""
import hashlib
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agents.jobs.job_engine import JobStateError, MasterJobEngine, STAGES, stage_receipt_signature


class SpyGateway:
    calls = 0

    def execute(self, capability, payload):
        self.calls += 1
        return {"status": "COMPLETED", "asset_ref": "asset:never-charged"}


def successful_outputs(job):
    scenes = job["script_result"]["scenes"]
    return {
        "GENERATION": {"generated_outputs": [
            {"scene": s["scene"], "result": {"status": "COMPLETED", "asset_ref": f"asset:scene-{s['scene']}"}}
            for s in scenes
        ]},
        "AUDIO": {"status": "COMPLETED", "asset_ref": "asset:audio"},
        "SUBTITLES": {"status": "COMPLETED", "asset_ref": "asset:subtitles"},
        "EDITING": {"status": "COMPLETED", "asset_ref": "asset:video"},
        "QUALITY_CONTROL": {"status": "PASSED", "checks": {
            "integrity": True, "visual": True, "audio": True, "subtitles": True,
            "render": True, "sync": True,
        }},
    }


class MasterJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dbpath = str(Path(self.temp.name) / "nested" / "jobs.sqlite3")
        self.spy = SpyGateway()
        self.engine = MasterJobEngine(self.dbpath, gateway=self.spy)

    def create(self, **kwargs):
        job = self.engine.create_job({"topic": "Space documentary", **kwargs})
        self.engine.save_job(job)
        return job

    def script(self, job):
        job["script_result"] = {"script": ["A real script"], "scenes": [
            {"scene": 1, "description": "One scene"},
            {"scene": 2, "description": "Two scenes"},
        ]}
        self.engine.save_job(job)
        return job

    def at_stage(self, job, stage):
        current = self.engine.run_job(job)
        for stg, output in successful_outputs(job).items():
            if current["current_stage"] == stage:
                return current
            if current["current_stage"] == stg:
                self.engine.record_stage_output(job["job_id"], stg, output)
                current = self.engine.run_job(self.engine.load_job(job["job_id"]))
        return current

    def test_empty_pipeline_never_masquerades_as_completed(self):
        job = self.create()
        output = self.engine.run_job(job)
        self.assertEqual((output["status"], output["current_stage"]), ("WAITING", "SCRIPT"))
        self.assertNotIn("final_output", output)
        self.assertEqual(output["stages"][0]["stage"], "CREATED")
        self.assertEqual(output["stages"][1]["stage"], "PLANNING")
        self.assertEqual(self.spy.calls, 0)

    def test_full_pipeline_requires_real_artifact_refs_and_explicit_quality_evidence(self):
        job = self.script(self.create())
        state = self.engine.run_job(job)
        self.assertEqual(state["current_stage"], "GENERATION")
        all_outputs = successful_outputs(job)
        for stage in ("GENERATION", "AUDIO", "SUBTITLES", "EDITING", "QUALITY_CONTROL"):
            self.assertEqual(state["status"], "WAITING")
            self.assertEqual(state["current_stage"], stage)
            next_state = self.engine.record_stage_output(job["job_id"], stage, all_outputs[stage])
            self.assertEqual(next_state["status"], "QUEUED")
            state = self.engine.run_job(next_state)
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["current_stage"], "COMPLETED")
        self.assertEqual(state["final_output"]["asset_ref"], "asset:video")
        self.assertEqual(len([x for x in state["stages"] if x["status"] == "PASSED"]), len(STAGES))
        self.assertEqual(self.spy.calls, 0)
        self.assertEqual(self.engine.load_job(job["job_id"])["status"], "COMPLETED")

    def test_fake_ready_or_empty_artifact_is_never_quality_success(self):
        job = self.script(self.create())
        state = self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "GENERATION", {
            "generated_outputs": [{"scene": 1, "result": {"status": "COMPLETED"}},
                                  {"scene": 2, "result": {"status": "READY"}}]})
        blocked = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(blocked["current_stage"], "GENERATION")
        self.assertEqual(blocked["status"], "WAITING")
        self.assertNotIn("final_output", blocked)

    def test_audio_has_to_exist(self):
        job = self.script(self.create())
        state = self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "GENERATION", successful_outputs(job)["GENERATION"])
        state = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(state["current_stage"], "AUDIO")
        self.engine.record_stage_output(job["job_id"], "AUDIO", {"status": "COMPLETED"})
        blocked = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(blocked["status"], "WAITING")
        self.assertEqual(blocked["current_stage"], "AUDIO")

    def test_failed_stage_stops_job_instead_of_advancing_to_completed(self):
        job = self.script(self.create())
        state = self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "GENERATION", {"status": "FAILED"})
        failed = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual((failed["status"], failed["current_stage"]), ("FAILED", "GENERATION"))
        self.assertNotIn("final_output", failed)
        self.assertEqual(self.engine.run_job(failed)["status"], "FAILED")

    def test_bad_quality_marks_failed(self):
        job = self.script(self.create())
        state = self.engine.run_job(job)
        for stage in ("GENERATION", "AUDIO", "SUBTITLES", "EDITING"):
            self.engine.record_stage_output(job["job_id"], stage, successful_outputs(job)[stage])
            state = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(state["current_stage"], "QUALITY_CONTROL")
        self.engine.record_stage_output(job["job_id"], "QUALITY_CONTROL", {"status": "PASSED", "checks": {"integrity": True}})
        blocked = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(blocked["status"], "WAITING")
        self.engine.record_stage_output(job["job_id"], "QUALITY_CONTROL", {"status": "REJECTED"})
        failed = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["current_stage"], "QUALITY_CONTROL")

    def test_skip_flags_are_honored_but_qc_still_required(self):
        job = self.create()
        job["brain_analysis"] = {"production_plan": {
            "script": False, "storyboard": False, "visual_generation": False,
            "audio": False, "subtitles": False, "editing": False, "quality_control": False,
        }}
        self.engine.save_job(job)
        blocked = self.engine.run_job(job)
        self.assertEqual(blocked["current_stage"], "QUALITY_CONTROL")
        self.assertEqual(blocked["status"], "WAITING")
        self.assertEqual(len([x for x in blocked["stages"] if x["status"] == "SKIPPED"]), 6)

    def test_idempotent_notifications_do_not_bump_revision(self):
        job = self.script(self.create())
        self.engine.run_job(job)
        output = successful_outputs(job)["GENERATION"]
        first = self.engine.record_stage_output(job["job_id"], "GENERATION", output)
        same = self.engine.record_stage_output(job["job_id"], "GENERATION", output)
        self.assertEqual(first["revision"], same["revision"])

    def test_notifications_reject_wrong_stage_and_terminal_job(self):
        job = self.create()
        with self.assertRaises(JobStateError):
            self.engine.record_stage_output(job["job_id"], "EDITING", {"status": "COMPLETED"})
        self.engine.cancel_job(job["job_id"])
        with self.assertRaises(JobStateError):
            self.engine.record_stage_output(job["job_id"], "SCRIPT", {"script": ["x"]})

    def test_invalid_requests_rejected(self):
        for req in ({}, {"topic": ""}, {"topic": 2}, {"topic": "x", "language": []},
                    {"topic": "x" * 1025}, {"topic": "ok", "duration": float("nan")}):
            with self.subTest(req=req), self.assertRaises(JobStateError):
                self.engine.create_job(req)

    def test_unserializable_output_and_large_payload_are_rejected(self):
        job = self.create()
        self.engine.run_job(job)
        with self.assertRaises(JobStateError):
            self.engine.record_stage_output(job["job_id"], "SCRIPT", {"script": {1, 2}})
        with self.assertRaises(JobStateError):
            self.engine.record_stage_output(job["job_id"], "SCRIPT", {"script": "x" * 300000})

    def test_optimistic_revision_conflict_prevents_stale_overwrite(self):
        job = self.create()
        stale = self.engine.load_job(job["job_id"])
        job["idea_result"] = {"ideas": ["safe"]}
        self.engine.save_job(job)
        with self.assertRaises(JobStateError):
            self.engine.save_job(stale)
        self.assertEqual(self.engine.load_job(job["job_id"])["idea_result"]["ideas"], ["safe"])

    def test_persistent_restart_keeps_waiting_state(self):
        job = self.create()
        waiting = self.engine.run_job(job)
        restarted = MasterJobEngine(self.dbpath)
        self.assertEqual(restarted.load_job(job["job_id"])["current_stage"], "SCRIPT")
        self.assertEqual(restarted.load_jobs()[0]["status"], "WAITING")
        self.assertEqual(waiting["revision"], restarted.load_job(job["job_id"])["revision"])

    def test_priority_queue_and_concurrency_limit(self):
        low = self.engine.create_job({"topic": "low"}, priority=1)
        high = self.engine.create_job({"topic": "high"}, priority=99)
        self.engine.save_job(low)
        self.engine.save_job(high)
        first = self.engine.claim_next("worker-A")
        self.assertEqual(first["job"]["job_id"], high["job_id"])
        self.assertEqual(self.engine.claim_next("worker-B")["job"]["job_id"], low["job_id"])
        self.assertIsNone(self.engine.claim_next("worker-C"))
        self.assertTrue(self.engine.renew_lease(high["job_id"], "worker-A", first["lease_token"]))
        self.assertFalse(self.engine.renew_lease(high["job_id"], "worker-B", first["lease_token"]))
        self.assertEqual(self.engine.release_claim(high["job_id"], "worker-A", first["lease_token"])["status"], "QUEUED")

    def test_lease_hash_not_plaintext_and_invalid_token_cannot_process(self):
        job = self.create()
        claim = self.engine.claim_next("worker-A")
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            stored = db.execute("SELECT lease_hash,snapshot FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        self.assertEqual(stored[0], hashlib.sha256(claim["lease_token"].encode()).hexdigest())
        self.assertNotIn(claim["lease_token"], stored[1])
        with self.assertRaises(JobStateError):
            self.engine.process_claim(job["job_id"], "worker-A", "invalid-token")
        self.assertEqual(self.engine.process_claim(job["job_id"], "worker-A", claim["lease_token"])["status"], "WAITING")

    def test_claim_from_two_engines_is_transactionally_exclusive(self):
        job = self.create()
        barrier = threading.Barrier(2)
        owners = []
        errors = []
        def worker(i):
            try:
                instance = MasterJobEngine(self.dbpath)
                barrier.wait(timeout=10)
                owners.append(instance.claim_next(f"worker-{i}"))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertFalse(errors)
        self.assertEqual(sum(x is not None for x in owners), 1)

    def test_cancel_active_job_revokes_on_next_safe_checkpoint(self):
        job = self.create()
        claim = self.engine.claim_next("worker-A")
        cancelled = self.engine.cancel_job(job["job_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["status"], "RUNNING")
        finished = self.engine.process_claim(job["job_id"], "worker-A", claim["lease_token"])
        self.assertEqual(finished["status"], "CANCELLED")

    def test_worker_crash_is_requeued_after_lease_expiration_without_provider_calls(self):
        engine = MasterJobEngine(self.dbpath, gateway=self.spy, lease_seconds=1, max_recoveries=1)
        job = engine.create_job({"topic": "recover"})
        engine.save_job(job)
        first = engine.claim_next("worker-crashed")
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time()-1, job["job_id"]))
        next_claim = engine.claim_next("worker-rescued")
        self.assertNotEqual(first["lease_token"], next_claim["lease_token"])
        with self.assertRaises(JobStateError):
            engine.process_claim(job["job_id"], "worker-crashed", first["lease_token"])
        engine.release_claim(job["job_id"], "worker-rescued", next_claim["lease_token"])
        final = engine.claim_next("worker-third")
        self.assertIsNotNone(final)
        self.assertEqual(self.spy.calls, 0)

    def test_excessive_crashes_fail_closed(self):
        engine = MasterJobEngine(self.dbpath, lease_seconds=1, max_recoveries=0)
        job = engine.create_job({"topic": "never settle"})
        engine.save_job(job)
        engine.claim_next("worker-crashed")
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time()-1, job["job_id"]))
        self.assertIsNone(engine.claim_next("worker-two"))
        self.assertEqual(engine.load_job(job["job_id"])["status"], "FAILED")

    def test_bad_storage_schema_is_refused(self):
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaises(JobStateError):
            MasterJobEngine(self.dbpath)

    def test_prompt_only_requests_are_normalized_for_existing_orchestrators(self):
        job = self.engine.create_job({"prompt": "Create a documentary"})
        self.assertEqual(job["request"]["topic"], "Create a documentary")
        self.assertEqual(self.engine.run_job(job)["status"], "WAITING")

    def test_fabricated_terminal_job_is_rejected_at_storage_boundary(self):
        job = self.engine.create_job({"topic": "space"})
        job["status"] = "COMPLETED"
        job["current_stage"] = "COMPLETED"
        with self.assertRaises(JobStateError):
            self.engine.save_job(job)

    def test_backpressure_limit_is_enforced_for_new_jobs(self):
        limited = MasterJobEngine(self.dbpath, max_queued=1)
        one = limited.create_job({"topic": "first"})
        two = limited.create_job({"topic": "second"})
        limited.save_job(one)
        with self.assertRaisesRegex(JobStateError, "queue full"):
            limited.save_job(two)
        self.assertIsNone(limited.load_job(two["job_id"]))

    def test_manual_recovery_requires_reason_and_corrected_output(self):
        job = self.script(self.create())
        self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "GENERATION", {"status": "FAILED"})
        failed = self.engine.run_job(self.engine.load_job(job["job_id"]))
        with self.assertRaises(JobStateError):
            self.engine.reopen_failed_job(job["job_id"], reason="")
        restarted = self.engine.reopen_failed_job(job["job_id"], reason="Operator reviewed provider incident")
        self.assertEqual(restarted["status"], "WAITING")
        self.assertEqual(len(restarted["recovery_history"]), 1)
        self.engine.record_stage_output(job["job_id"], "GENERATION", successful_outputs(job)["GENERATION"])
        next_state = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual((next_state["status"], next_state["current_stage"]), ("WAITING", "AUDIO"))
        self.assertEqual(self.spy.calls, 0)

    def test_failed_individual_provider_output_halts_pipeline(self):
        job = self.script(self.create())
        self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "GENERATION", {
            "generated_outputs": [
                {"scene": 1, "result": {"status": "COMPLETED", "asset_ref": "asset:ok"}},
                {"scene": 2, "result": {"status": "FAILED", "reason": "NOT_BILLED"}},
            ]})
        failed = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["current_stage"], "GENERATION")

    def test_legacy_migration_never_trusts_historical_success_marker(self):
        old_path = Path(self.temp.name) / "old.json"
        legacy_id = "2a5c3d96-667b-4487-981c-4fffa99b4167"
        old_json = [
            {"job_id": legacy_id, "status": "COMPLETED", "request": {"topic": "history"},
             "stage_result": {"status": "READY", "stage": "COMPLETED"}},
            {"status": "COMPLETED", "request": {"topic": "invalid without id"}},
        ]
        old_path.write_text(json.dumps(old_json), encoding="utf-8")
        dry = self.engine.import_legacy_jobs(old_path)
        self.assertEqual(dry, {"scanned": 2, "imported": 1, "skipped": 0, "invalid": 1})
        self.assertIsNone(self.engine.load_job(legacy_id))
        real = self.engine.import_legacy_jobs(old_path, dry_run=False)
        self.assertEqual(real["imported"], 1)
        imported = self.engine.load_job(legacy_id)
        self.assertEqual(imported["status"], "CREATED")
        self.assertTrue(imported["legacy_imported"])
        self.assertNotIn("final_output", imported)
        self.assertEqual(self.engine.import_legacy_jobs(old_path, dry_run=False)["skipped"], 1)
        self.assertEqual(json.loads(old_path.read_text(encoding="utf-8")), old_json)

    def test_cli_submit_worker_record_status_without_secrets(self):
        request = Path(self.temp.name) / "input.json"
        request.write_text(json.dumps({"topic": "CLI documentary"}), encoding="utf-8")
        def cli(*args):
            done = subprocess.run([sys.executable, "-m", "agents.jobs.job_cli", "--db", self.dbpath, *args],
                                  capture_output=True, text=True, check=True)
            return json.loads(done.stdout)
        sent = cli("submit", str(request))
        self.assertEqual(sent["status"], "CREATED")
        first = cli("worker")
        self.assertEqual(first["processed"][0]["current_stage"], "SCRIPT")
        self.assertEqual(first["processed"][0]["status"], "WAITING")
        evidence = Path(self.temp.name) / "script.json"
        evidence.write_text(json.dumps({"script": ["text"], "scenes": [{"scene": 1, "description": "image"}]}), encoding="utf-8")
        cli("record", sent["job_id"], "SCRIPT", str(evidence))
        second = cli("worker")
        self.assertEqual(second["processed"][0]["current_stage"], "GENERATION")
        safe = cli("status", sent["job_id"])
        self.assertEqual(safe["status"], "WAITING")
        self.assertNotIn("request", safe)
        self.assertNotIn("lease_token", str(safe))

    def test_skipped_editing_still_requires_explicit_final_artifact(self):
        job = self.create()
        job["brain_analysis"] = {"production_plan": {
            "script": False, "storyboard": False, "visual_generation": False,
            "audio": False, "subtitles": False, "editing": False,
        }}
        self.engine.save_job(job)
        self.engine.run_job(job)
        self.engine.record_stage_output(job["job_id"], "QUALITY_CONTROL", {
            "status": "PASSED", "checks": {"integrity": True},
        })
        waiting = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(waiting["current_stage"], "COMPLETED")
        self.assertEqual(waiting["status"], "WAITING")
        self.engine.record_stage_output(job["job_id"], "COMPLETED", {
            "asset_ref": "asset:document",
        })
        completed = self.engine.run_job(self.engine.load_job(job["job_id"]))
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["final_output"]["asset_ref"], "asset:document")

    def test_runtime_no_external_provider_side_effects_even_with_stage_data(self):
        job = self.script(self.create())
        self.engine.run_job(job)
        self.assertEqual(self.spy.calls, 0)
        self.assertNotIn("READY", ("COMPLETED",))


    def test_idempotency_deduplicates_exact_retry_across_engine_restart(self):
        job = self.engine.submit_request({"topic": "repeat", "language": "ur"},
                                         idempotency_key="req-1", priority=75)
        restarted = MasterJobEngine(self.dbpath)
        duplicate = restarted.submit_request({"language": "ur", "topic": "repeat"},
                                              idempotency_key="req-1", priority=75)
        self.assertEqual(duplicate["job_id"], job["job_id"])
        self.assertEqual(duplicate["revision"], job["revision"])
        self.assertEqual(len(restarted.load_jobs()), 1)
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            row = db.execute("SELECT key_hash,request_hash FROM submission_keys").fetchone()
            self.assertNotEqual(row[0], "req-1")
            self.assertEqual(len(row[1]), 64)
        self.assertEqual(self.spy.calls, 0)

    def test_idempotency_key_conflict_and_invalid_keys_fail_closed(self):
        self.engine.submit_request({"topic": "one"}, idempotency_key="req-unique")
        for request, priority in (({"topic": "two"}, 50), ({"topic": "one"}, 30)):
            with self.subTest(request=request, priority=priority), self.assertRaises(JobStateError):
                self.engine.submit_request(request, idempotency_key="req-unique", priority=priority)
        for key in ("", "space here", "\n", "!unsafe", "x" * 129):
            with self.subTest(key=key), self.assertRaises(JobStateError):
                self.engine.submit_request({"topic": "one"}, idempotency_key=key)
        self.assertEqual(len(self.engine.load_jobs()), 1)

    def test_same_idempotency_key_concurrent_submit_only_creates_one_job(self):
        jobs = []
        errors = []
        barrier = threading.Barrier(6)

        def worker():
            try:
                engine = MasterJobEngine(self.dbpath)
                barrier.wait(timeout=10)
                jobs.append(engine.submit_request({"topic": "concurrent"}, idempotency_key="same-key"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=15)
        self.assertFalse(errors)
        self.assertEqual(len(jobs), 6)
        self.assertEqual(len(set(x["job_id"] for x in jobs)), 1)
        self.assertEqual(len(self.engine.load_jobs()), 1)

    def test_audit_event_stream_contains_no_private_prompts_or_worker_tokens(self):
        job = self.engine.create_job({"topic": "private sensitive prompt"})
        self.engine.save_job(job)
        claim = self.engine.claim_next("worker-A")
        self.engine.process_claim(job["job_id"], "worker-A", claim["lease_token"])
        self.engine.record_stage_output(job["job_id"], "SCRIPT", {
            "script": ["private sensitive script"], "scenes": [{"scene": 1, "description": "scene"}],
        })
        events = self.engine.job_events(job["job_id"])
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(events[-1]["event"], "WRITE")
        self.assertEqual([x["seq"] for x in events], sorted(x["seq"] for x in events))
        with closing(sqlite3.connect(self.dbpath)) as conn, conn as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_events").fetchone()[0], len(events))
        self.assertNotIn("private sensitive", json.dumps(events))
        self.assertNotIn(claim["lease_token"], json.dumps(events))
        self.assertEqual(self.engine.job_events(job["job_id"], limit=2), events[-2:])
        with self.assertRaises(JobStateError):
            self.engine.job_events(job["job_id"], limit=501)

    def test_direct_save_cannot_forge_any_stage_or_quality_evidence(self):
        job = self.create()
        for name, value in (
            ("current_stage", "COMPLETED"), ("status", "WAITING"),
            ("quality_control_result", {"status": "PASSED"}),
            ("final_result", {"asset_ref": "asset:fake"}),
        ):
            fake = dict(self.engine.load_job(job["job_id"]))
            fake[name] = value
            with self.subTest(name=name), self.assertRaises(JobStateError):
                self.engine.save_job(fake)
        waiting = self.engine.run_job(job)
        self.assertEqual(waiting["status"], "WAITING")
        with self.assertRaises(JobStateError):
            self.engine.save_job(waiting)
        # Legacy run_job also refuses to insert a fabricated terminal job.
        forged = self.engine.create_job({"topic": "forged"})
        forged.update({"current_stage": "COMPLETED", "status": "COMPLETED",
            "quality_plan": {"final_quality_status": "PASSED"},
            "final_output": {"status": "READY", "asset_ref": "asset:unverified"},
            "stage_result": {"stage": "COMPLETED", "status": "READY"}})
        with self.assertRaises(JobStateError):
            self.engine.run_job(forged)

    def test_persistence_rejects_nested_credentials_and_boolean_worker_limits(self):
        with self.assertRaises(JobStateError):
            self.engine.create_job({"topic": "x", "nested": {"credentials": {"password": "abc"}}})
        job = self.create()
        self.engine.run_job(job)
        with self.assertRaises(JobStateError):
            self.engine.record_stage_output(job["job_id"], "SCRIPT", {
                "script": ["x"], "auth-token": "SECRET"})
        with self.assertRaises(ValueError):
            MasterJobEngine(self.dbpath, max_concurrent=True)
        with self.assertRaises(JobStateError):
            self.engine.claim_next("bad worker\n")

    def test_cli_idempotent_submit_events_and_redacted_final_status(self):
        request = Path(self.temp.name) / "request.json"
        request.write_text(json.dumps({"topic": "private CLI topic"}), encoding="utf-8")
        def cli(*args):
            proc = subprocess.run([sys.executable, "-m", "agents.jobs.job_cli", "--db", self.dbpath, *args],
                                  check=True, capture_output=True, text=True)
            return json.loads(proc.stdout)
        first = cli("submit", str(request), "--idempotency-key", "stable-request")
        again = cli("submit", str(request), "--idempotency-key", "stable-request")
        self.assertEqual(first["job_id"], again["job_id"])
        cli("worker")
        events = cli("events", first["job_id"])["events"]
        self.assertTrue(any(x["event"] == "CLAIM" for x in events))
        status = cli("status", first["job_id"])
        self.assertNotIn("private CLI topic", json.dumps(status))



    def test_signed_stage_receipt_authenticates_payload_job_and_stage(self):
        key = b"a" * 32
        engine = MasterJobEngine(self.dbpath, receipt_key=key)
        job = engine.create_job({"topic": "signed documentary"})
        engine.save_job(job)
        engine.run_job(job)
        output = {"script": ["Verified script"], "scenes": [{"scene": 1, "description": "image"}]}
        valid_sig = stage_receipt_signature(job["job_id"], "SCRIPT", output, key)
        for bad_sig in (None, "0" * 64, "not-hex"):
            with self.subTest(signature=bad_sig), self.assertRaises(JobStateError):
                engine.record_stage_output(job["job_id"], "SCRIPT", output, receipt_signature=bad_sig)
        with self.assertRaises(JobStateError):
            engine.record_stage_output(job["job_id"], "SCRIPT", {**output, "script": ["tampered"]},
                                       receipt_signature=valid_sig)
        with self.assertRaises(JobStateError):
            engine.record_stage_output(job["job_id"], "SCRIPT", output,
                receipt_signature=stage_receipt_signature("wrong-job", "SCRIPT", output, key))
        stored = engine.record_stage_output(job["job_id"], "SCRIPT", output, receipt_signature=valid_sig)
        self.assertEqual(stored["status"], "QUEUED")
        with closing(sqlite3.connect(self.dbpath)) as db:
            # A receipt is a transient proof; never put the HMAC in persistent state.
            data = db.execute("SELECT snapshot FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0]
        self.assertNotIn(valid_sig, data)
        self.assertNotIn(key.hex(), data)
        state = engine.run_job(engine.load_job(job["job_id"]))
        self.assertEqual((state["current_stage"], state["status"]), ("GENERATION", "WAITING"))
        with self.assertRaises(JobStateError):
            engine.record_stage_output(job["job_id"], "GENERATION", {"status": "COMPLETED"},
                                       receipt_signature=valid_sig)
        with self.assertRaises(ValueError):
            MasterJobEngine(self.dbpath, receipt_key=b"short")

    def test_signatures_identical_for_ordered_json_and_different_across_stages(self):
        key = b"b" * 32
        a = {"status": "COMPLETED", "asset_ref": "asset:one"}
        b = {"asset_ref": "asset:one", "status": "COMPLETED"}
        self.assertEqual(stage_receipt_signature("id", "AUDIO", a, key),
                         stage_receipt_signature("id", "AUDIO", b, key))
        self.assertNotEqual(stage_receipt_signature("id", "AUDIO", a, key),
                            stage_receipt_signature("id", "EDITING", b, key))



    def test_signed_mode_blocks_unsigned_preloaded_script(self):
        key = b"c" * 32
        engine = MasterJobEngine(self.dbpath, receipt_key=key)
        job = engine.create_job({"topic": "signed-only"})
        job["script_result"] = {"script": ["unverified"]}
        with self.assertRaises(JobStateError):
            engine.save_job(job)
        with self.assertRaises(JobStateError):
            engine.run_job(job)
        self.assertIsNone(engine.load_job(job["job_id"]))

    def test_cli_signed_mode_requires_receipt_and_uses_env_without_key_leak(self):
        key = b"d" * 32
        env = {**os.environ, "AI_CREATOR_JOB_RECEIPT_KEY_HEX": key.hex()}
        request = Path(self.temp.name) / "signed.json"
        request.write_text(json.dumps({"topic": "signed CLI"}), encoding="utf-8")
        def cli(*args, ok=True):
            proc = subprocess.run([sys.executable, "-m", "agents.jobs.job_cli", "--db", self.dbpath, *args],
                                  capture_output=True, text=True, env=env)
            if ok:
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn(key.hex(), proc.stderr)
            return json.loads(proc.stderr)
        job = cli("submit", str(request))
        cli("worker")
        output = {"script": ["signed"], "scenes": [{"scene": 1, "description": "generated"}]}
        file = Path(self.temp.name) / "output.json"
        file.write_text(json.dumps(output), encoding="utf-8")
        err = cli("record", job["job_id"], "SCRIPT", str(file), ok=False)
        self.assertEqual(err["error"], "JobStateError")
        sig = stage_receipt_signature(job["job_id"], "SCRIPT", output, key)
        result = cli("record", job["job_id"], "SCRIPT", str(file), "--signature", sig)
        self.assertEqual(result["status"], "QUEUED")
        cli("worker")
        self.assertEqual(cli("status", job["job_id"])["current_stage"], "GENERATION")


if __name__ == "__main__":
    unittest.main()
