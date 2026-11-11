"""Prove local engine handoff using decodable synthetic media (zero API credits).

Requires ffmpeg and ffprobe executables. Runs with a private temporary directory.
Does not validate connected live cloud providers or perceptual output quality.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import wave
import zlib
from pathlib import Path

from agents.jobs.artifact_integrity import ArtifactVerifier
from agents.jobs.job_engine import MasterJobEngine, stage_receipt_signature


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(path):
    row = b"\0\x16\x50\xa0"  # filter 0, blue RGB pixel
    png = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + png_chunk(b"IDAT", zlib.compress(row)) + png_chunk(b"IEND", b""))
    path.write_bytes(png)


def main():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("SKIPPED: install ffmpeg and ffprobe for actual decode acceptance")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        media = root / "media"
        media.mkdir()
        make_png(media / "scene.png")
        with wave.open(str(media / "speech.wav"), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"".join(struct.pack("<h", int(800 * math.sin(i / 25))) for i in range(4000)))
        (media / "subtitle.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nHello\n", encoding="utf-8")
        video = media / "render.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=2:d=1",
                        "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(video)],
                       check=True, timeout=30)
        for source in (media / "scene.png", media / "speech.wav", video):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-i", str(source), "-f", "null", "-"], check=True, timeout=30)
        checker = ArtifactVerifier(media)
        key = b"local-test-receipt-key-must-be-32-bytes-or-more"
        engine = MasterJobEngine(root / "jobs.sqlite3", artifact_root=media, receipt_key=key)
        job = engine.create_job({"topic": "synthetic offline acceptance"})
        engine.save_job(job)
        current = engine.run_job(job)
        stages = {
            "SCRIPT": {"script": ["Synthetic movie script"],
                       "scenes": [{"scene": 1, "description": "Synthetic intro"}]},
            "GENERATION": {"generated_outputs": [{"scene": 1, "result": {
                "status": "COMPLETED", "asset_ref": "asset:scene", "evidence": checker.inspect("scene.png", "image/png")}}]},
            "AUDIO": {"status": "COMPLETED", "asset_ref": "asset:audio",
                      "evidence": checker.inspect("speech.wav", "audio/wav")},
            "SUBTITLES": {"status": "COMPLETED", "asset_ref": "asset:subtitle",
                          "evidence": checker.inspect("subtitle.vtt", "text/vtt")},
            "EDITING": {"status": "COMPLETED", "asset_ref": "asset:video",
                        "evidence": checker.inspect("render.mp4", "video/mp4")},
            "QUALITY_CONTROL": {"status": "PASSED", "checks": {name: True for name in
                               ("integrity", "visual", "audio", "subtitles", "render", "sync")}},
        }
        for stage, output in stages.items():
            assert current["status"] == "WAITING" and current["current_stage"] == stage, current
            engine.record_stage_output(job["job_id"], stage, output,
                        receipt_signature=stage_receipt_signature(job["job_id"], stage, output, key))
            current = engine.run_job(engine.load_job(job["job_id"]))
        assert current["status"] == "COMPLETED", current
        assert current["final_output"]["asset_ref"] == "asset:video"
        assert len(engine.job_events(job["job_id"])) >= 10
        print("PASS: synthetic PNG/WAV/MP4 decoded; signed, hash-verified six-stage handoff completed")


if __name__ == "__main__":
    main()
