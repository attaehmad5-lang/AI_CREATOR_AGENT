import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/execution_report.json"
OUTPUT_FILE = "projects/generation/asset_manifest.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

assets = []

for job in data["jobs"]:

    media_type = job["type"].lower()

    if media_type == "image":
        extension = "png"
        folder = "output/images"
    elif media_type == "video":
        extension = "mp4"
        folder = "output/videos"
    elif media_type == "voice":
        extension = "wav"
        folder = "output/voice"
    else:
        extension = "dat"
        folder = "output/assets"

    asset_id = "ASSET_" + job["job_id"]

    assets.append({
        "asset_id": asset_id,
        "job_id": job["job_id"],
        "project": data["project"],
        "scene": job["scene"],
        "media_type": media_type.upper(),
        "required_capability": job["required_capability"],
        "provider": job["selected_provider"],
        "status": "WAITING_FOR_PROVIDER",
        "file_path": None,
        "planned_output_path": f"{folder}/{asset_id}.{extension}",
        "attempts": 0,
        "max_attempts": job["max_attempts"],
        "created_at": datetime.now().isoformat()
    })

output = {
    "project": data["project"],
    "engine": "Advanced Asset Management Engine",
    "version": "1.0",
    "status": "READY",
    "total_assets": len(assets),
    "images": len([a for a in assets if a["media_type"] == "IMAGE"]),
    "videos": len([a for a in assets if a["media_type"] == "VIDEO"]),
    "voices": len([a for a in assets if a["media_type"] == "VOICE"]),
    "completed": 0,
    "waiting_for_provider": len([
        a for a in assets
        if a["status"] == "WAITING_FOR_PROVIDER"
    ]),
    "assets": assets,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("      ADVANCED ASSET MANAGER")
print("===================================")
print()
print("Project:", output["project"])
print("Total assets:", output["total_assets"])
print("Images:", output["images"])
print("Videos:", output["videos"])
print("Voices:", output["voices"])
print("Waiting for provider:", output["waiting_for_provider"])
print("Completed:", output["completed"])
print()
print("Status:", output["status"])
print()
print("Asset manifest created successfully.")
print("Saved to:", OUTPUT_FILE)
