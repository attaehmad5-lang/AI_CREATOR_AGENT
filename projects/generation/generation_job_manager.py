import json
import os
from datetime import datetime

VISUAL_FILE = "projects/visuals/visual_prompts.json"
VOICE_FILE = "projects/voice/voice_prompts.json"
OUTPUT_FILE = "projects/generation/generation_jobs.json"

with open(VISUAL_FILE, "r", encoding="utf-8") as f:
    visuals = json.load(f)

with open(VOICE_FILE, "r", encoding="utf-8") as f:
    voices = json.load(f)

jobs = []

for visual in visuals["visuals"]:
    scene = visual["scene"]

    jobs.append({
        "job_id": f"IMG_SCENE_{scene:02d}",
        "type": "IMAGE",
        "scene": scene,
        "title": visual["title"],
        "prompt": visual["image_prompt"],
        "aspect_ratio": "16:9",
        "resolution": "4K",
        "provider": "NOT_CONNECTED",
        "status": "QUEUED"
    })

    jobs.append({
        "job_id": f"VIDEO_SCENE_{scene:02d}",
        "type": "VIDEO",
        "scene": scene,
        "title": visual["title"],
        "prompt": visual["video_prompt"],
        "aspect_ratio": "16:9",
        "resolution": "4K",
        "frame_rate": "24fps",
        "provider": "NOT_CONNECTED",
        "status": "QUEUED"
    })

for voice in voices["voice_items"]:
    scene = voice["scene"]

    jobs.append({
        "job_id": f"VOICE_SCENE_{scene:02d}",
        "type": "VOICE",
        "scene": scene,
        "title": voice["title"],
        "narration": voice["narration"],
        "voice_style": voice["voice_style"],
        "delivery": voice["delivery"],
        "provider": "NOT_CONNECTED",
        "status": "QUEUED"
    })

output = {
    "project": visuals["project"],
    "status": "READY",
    "engine": "Generation Job Manager",
    "total_jobs": len(jobs),
    "image_jobs": len([j for j in jobs if j["type"] == "IMAGE"]),
    "video_jobs": len([j for j in jobs if j["type"] == "VIDEO"]),
    "voice_jobs": len([j for j in jobs if j["type"] == "VOICE"]),
    "jobs": jobs,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("      GENERATION JOB MANAGER")
print("===================================")
print()
print("Project:", output["project"])
print("Image jobs:", output["image_jobs"])
print("Video jobs:", output["video_jobs"])
print("Voice jobs:", output["voice_jobs"])
print("Total jobs:", output["total_jobs"])
print()
print("Status:", output["status"])
print()
print("Generation job file created successfully.")
print("Saved to:", OUTPUT_FILE)
