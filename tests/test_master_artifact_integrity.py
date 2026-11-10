"""Offline production-handoff tests: never contact remote providers or spend credits."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from agents.jobs.artifact_integrity import ArtifactIntegrityError, ArtifactVerifier
from agents.jobs.job_engine import MasterJobEngine


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-png-bytes"
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + b"fixture"
MP4 = (24).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 16
VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"


class ArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.verifier = ArtifactVerifier(self.media)

    def file(self, name, data, kind):
        target = self.media / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self.verifier.inspect(name, kind)

    def test_correct_content_is_hashed_with_exact_size(self):
        proof = self.file("images/image.png", PNG, "image/png")
        self.assertEqual(proof["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(proof["size_bytes"], len(PNG))
        self.assertTrue(self.verifier.verify(proof, allowed_types={"image/png"}))

    def test_hash_tampering_and_size_tampering_are_rejected(self):
        proof = self.file("scene.png", PNG, "image/png")
        (self.media / "scene.png").write_bytes(PNG + b"extra")
        with self.assertRaises(ArtifactIntegrityError):
            self.verifier.verify(proof, allowed_types={"image/png"})
        other = self.file("new.png", PNG, "image/png")
        other["size_bytes"] += 1
        with self.assertRaises(ArtifactIntegrityError):
            self.verifier.verify(other, allowed_types={"image/png"})

    def test_path_escape_and_symlink_rejected(self):
        for bad in ("../secret.txt", "a/../scene.png", "/tmp/scene.png", "C:\\secret.png", "bad//path", "./scene.png", "foo\\bar.png"):
            with self.subTest(bad=bad), self.assertRaises(ArtifactIntegrityError):
                self.verifier.inspect(bad, "image/png")
        outside = self.root / "outside.png"
        outside.write_bytes(PNG)
        if hasattr(os, "symlink"):
            try:
                (self.media / "link.png").symlink_to(outside)
            except (OSError, NotImplementedError):
                pass
            else:
                with self.assertRaises(ArtifactIntegrityError):
                    self.verifier.inspect("link.png", "image/png")

    def test_wrong_mime_and_empty_and_oversize_fail_closed(self):
        (self.media / "broken.png").write_bytes(b"not PNG")
        (self.media / "empty.png").write_bytes(b"")
        for name in ("broken.png", "empty.png"):
            with self.assertRaises(ArtifactIntegrityError):
                self.verifier.inspect(name, "image/png")
        with self.assertRaises(ArtifactIntegrityError):
            ArtifactVerifier(self.media, max_file_bytes=2).inspect("broken.png", "image/png")
        with self.assertRaises(ArtifactIntegrityError):
            self.verifier.inspect("broken.png", "application/octet-stream")

    def test_extra_metadata_in_evidence_is_rejected(self):
        proof = self.file("safe.png", PNG, "image/png")
        proof["arbitrary"] = "unexpected"
        with self.assertRaises(ArtifactIntegrityError):
            self.verifier.verify(proof, allowed_types={"image/png"})

    def test_stage_enforces_type_separation(self):
        proof = self.file("audio.wav", WAV, "audio/wav")
        with self.assertRaises(ArtifactIntegrityError):
            self.verifier.verify(proof, allowed_types={"image/png"})

    def test_strict_pipeline_verifies_real_local_file_bytes_at_each_stage(self):
        engine = MasterJobEngine(self.root / "db.sqlite3", artifact_root=self.media)
        job = engine.create_job({"topic": "Media workflow"})
        job["script_result"] = {"script": ["Body"], "scenes": [{"scene": 1, "description": "Intro"}]}
        engine.save_job(job)
        state = engine.run_job(job)
        self.assertEqual(state["current_stage"], "GENERATION")
        outputs = {
            "GENERATION": {"generated_outputs": [{"scene": 1, "result": {"status": "COMPLETED", "asset_ref": "scene:1", "evidence": self.file("image.png", PNG, "image/png")}}]},
            "AUDIO": {"status": "COMPLETED", "asset_ref": "audio:1", "evidence": self.file("audio.wav", WAV, "audio/wav")},
            "SUBTITLES": {"status": "COMPLETED", "asset_ref": "subtitle:1", "evidence": self.file("caption.vtt", VTT, "text/vtt")},
            "EDITING": {"status": "COMPLETED", "asset_ref": "render:1", "evidence": self.file("render.mp4", MP4, "video/mp4")},
            "QUALITY_CONTROL": {"status": "PASSED", "checks": {k: True for k in ("integrity", "visual", "audio", "subtitles", "render", "sync")}},
        }
        for stage, output in outputs.items():
            self.assertEqual(state["current_stage"], stage)
            engine.record_stage_output(job["job_id"], stage, output)
            state = engine.run_job(engine.load_job(job["job_id"]))
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["final_output"]["asset_ref"], "render:1")

    def test_missing_integrity_evidence_blocks_even_with_fake_completed_flag(self):
        engine = MasterJobEngine(self.root / "db.sqlite3", artifact_root=self.media)
        job = engine.create_job({"topic": "Media workflow"})
        job["script_result"] = {"script": ["Body"], "scenes": [{"scene": 1, "description": "Intro"}]}
        engine.save_job(job)
        engine.run_job(job)
        engine.record_stage_output(job["job_id"], "GENERATION", {"generated_outputs": [
            {"scene": 1, "result": {"status": "COMPLETED", "asset_ref": "fake:1"}}]})
        state = engine.run_job(engine.load_job(job["job_id"]))
        self.assertEqual(state["status"], "WAITING")
        self.assertEqual(state["stage_result"]["reason"], "VERIFIED_MEDIA_EVIDENCE_REQUIRED")

    def test_media_mutation_after_receipt_fails_instead_of_succeeding(self):
        engine = MasterJobEngine(self.root / "db.sqlite3", artifact_root=self.media)
        job = engine.create_job({"topic": "Media workflow"})
        job["script_result"] = {"script": ["Body"], "scenes": [{"scene": 1, "description": "Intro"}]}
        engine.save_job(job)
        engine.run_job(job)
        output = {"generated_outputs": [{"scene": 1, "result": {"status": "COMPLETED", "asset_ref": "scene:1", "evidence": self.file("image.png", PNG, "image/png")}}]}
        engine.record_stage_output(job["job_id"], "GENERATION", output)
        (self.media / "image.png").write_bytes(PNG + b"changed")
        state = engine.run_job(engine.load_job(job["job_id"]))
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["stage_result"]["reason"], "MEDIA_INTEGRITY_FAILED")

    def test_strict_mode_rejects_non_directory_root(self):
        with self.assertRaises(ArtifactIntegrityError):
            MasterJobEngine(self.root / "db.sqlite3", artifact_root=self.root / "nonexistent")


if __name__ == '__main__':
    unittest.main()


class DurableExternalDispatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        class MockGateway:
            calls = 0
            def execute(self, capability, payload):
                self.calls += 1
                return {"status": "QUEUED", "request_id": "fake-request"}
        self.gateway = MockGateway()
        self.engine = MasterJobEngine(self.root / "db.sqlite3", gateway=self.gateway)
        self.job = self.engine.create_job({"topic": "Scene generation"})
        self.engine.save_job(self.job)
        self.state = self.engine.run_job(self.job)
        self.assertEqual(self.state["current_stage"], "SCRIPT")
        # A script and scenes advance the pipeline to generation.
        self.engine.record_stage_output(self.job["job_id"], "SCRIPT", {"script": ["test"],
            "scenes": [{"scene": 1, "description": "A scene"}]})
        self.state = self.engine.run_job(self.engine.load_job(self.job["job_id"]))
        self.assertEqual((self.state["status"], self.state["current_stage"]), ("WAITING", "GENERATION"))

    def test_no_external_call_without_explicit_authorization(self):
        with self.assertRaisesRegex(ValueError, "disabled by default"):
            self.engine.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
                                                "text_to_image", {"prompt": "image"})
        with self.assertRaises(ValueError):
            self.engine.execute_capability("text_to_image", {"prompt": "image"})
        self.assertEqual(self.gateway.calls, 0)
        self.assertEqual(self.engine.external_dispatch_history(self.job["job_id"]), [])

    def test_single_authorized_dispatch_cannot_replay_after_restart(self):
        first = self.engine.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
                                                    "text_to_image", {"prompt": "sensitive-private-prompt"}, authorize_spend=True)
        self.assertEqual(first["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(first["provider_status"], "QUEUED")
        self.assertEqual(self.gateway.calls, 1)
        restarted = MasterJobEngine(self.root / "db.sqlite3", gateway=self.gateway)
        with self.assertRaisesRegex(ValueError, "already submitted"):
            restarted.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
                                             "text_to_image", {"prompt": "sensitive-private-prompt"}, authorize_spend=True)
        self.assertEqual(self.gateway.calls, 1)
        history = restarted.external_dispatch_history(self.job["job_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "RECONCILE")
        self.assertNotIn("sensitive-private-prompt", json.dumps(history))

    def test_wrong_stage_credentials_invalid_payload_cannot_call_gateway(self):
        attempts = [
            ("AUDIO", "text_to_speech", {"text": "hello"}),
            ("GENERATION", "text_to_speech", {"text": "hello"}),
            ("GENERATION", "text_to_image", {"api_key": "secret"}),
            ("GENERATION", "text_to_image", {"prompt": object()}),
        ]
        for stage, capability, payload in attempts:
            with self.subTest(stage=stage, payload=payload), self.assertRaises(ValueError):
                self.engine.dispatch_external_stage(self.job["job_id"], stage, "scene-1",
                                                    capability, payload, authorize_spend=True)
        self.assertEqual(self.gateway.calls, 0)

    def test_provider_exception_is_ambiguous_and_never_retried(self):
        def failed(capability, payload):
            self.gateway.calls += 1
            raise RuntimeError("upstream might have accepted paid request")
        self.gateway.execute = failed
        result = self.engine.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
                                                      "text_to_image", {"prompt": "hi"}, authorize_spend=True)
        self.assertEqual(result["provider_status"], "UNKNOWN")
        self.assertEqual(self.gateway.calls, 1)
        with self.assertRaises(ValueError):
            self.engine.dispatch_external_stage(self.job["job_id"], "GENERATION", "scene-1",
                                                "text_to_image", {"prompt": "hi"}, authorize_spend=True)
        self.assertEqual(self.gateway.calls, 1)
