import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/generation_state.json"
OUTPUT_FILE = "projects/generation/retry_state.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

jobs = []

for state in data["states"]:
    jobs.append({
        "asset_id": state["asset_id"],
        "job_id": state["job_id"],
        "scene": state["scene"],
        "media_type": state["media_type"],
        "state": state["state"],
        "attempts": state["attempts"],
        "max_attempts": state["max_attempts"],
        "retry_available": state["attempts"] < state["max_attempts"],
        "retry_count": 0,
        "last_error": None,
        "next_action": "WAIT_FOR_PROVIDER",
        "updated_at": datetime.now().isoformat()
    })

output = {
    "project": data["project"],
    "engine": "Advanced Retry & Failure Recovery Engine",
    "version": "1.0",
    "status": "READY",
    "total_jobs": len(jobs),
    "retry_available": len([
        j for j in jobs
        if j["retry_available"]
    ]),
    "failed": 0,
    "retrying": 0,
    "waiting_for_provider": len(jobs),
    "jobs": jobs,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   RETRY & RECOVERY ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Retry available:", output["retry_available"])
print("Failed:", output["failed"])
print("Retrying:", output["retrying"])
print("Waiting for provider:", output["waiting_for_provider"])
print()
print("Status:", output["status"])
print()
print("Retry state created successfully.")
print("Saved to:", OUTPUT_FILE)
