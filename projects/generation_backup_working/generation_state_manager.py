import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/asset_manifest.json"
OUTPUT_FILE = "projects/generation/generation_state.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

states = []

for asset in data["assets"]:

    states.append({
        "asset_id": asset["asset_id"],
        "job_id": asset["job_id"],
        "scene": asset["scene"],
        "media_type": asset["media_type"],
        "provider": asset["provider"],

        "state": "QUEUED",

        "previous_state": None,

        "attempts": 0,
        "max_attempts": asset["max_attempts"],

        "progress": 0,

        "error": None,

        "output_file": None,

        "state_history": [
            {
                "state": "QUEUED",
                "timestamp": datetime.now().isoformat()
            }
        ],

        "updated_at": datetime.now().isoformat()
    })

output = {
    "project": data["project"],
    "engine": "Advanced Generation State Manager",
    "version": "1.0",

    "status": "READY",

    "total_assets": len(states),

    "queued": len([
        s for s in states
        if s["state"] == "QUEUED"
    ]),

    "processing": 0,

    "completed": 0,

    "failed": 0,

    "retrying": 0,

    "states": states,

    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   GENERATION STATE MANAGER")
print("===================================")
print()

print("Project:", output["project"])
print("Total assets:", output["total_assets"])
print("Queued:", output["queued"])
print("Processing:", output["processing"])
print("Completed:", output["completed"])
print("Failed:", output["failed"])
print("Retrying:", output["retrying"])

print()
print("Status:", output["status"])
print()

print("Generation state database created successfully.")
print("Saved to:", OUTPUT_FILE)
