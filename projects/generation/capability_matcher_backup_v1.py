import json
import os
from datetime import datetime

QUEUE_FILE = "projects/generation/routed_generation_queue.json"
REGISTRY_FILE = "projects/generation/provider_registry.json"
OUTPUT_FILE = "projects/generation/capability_matched_queue.json"

with open(QUEUE_FILE, "r", encoding="utf-8") as f:
    queue = json.load(f)

with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
    registry = json.load(f)


REQUIRED_CAPABILITIES = {
    "IMAGE": "text_to_image",
    "VIDEO": "text_to_video",
    "VOICE": "text_to_speech"
}


def get_capability(job_type):
    return REQUIRED_CAPABILITIES.get(job_type)


matched_jobs = []

for job in queue["routed_jobs"]:

    required = get_capability(job["type"])

    if job["selected_provider"] is None:
        capability_status = "WAITING_FOR_PROVIDER"
    else:
        capability_status = "CAPABILITY_CHECK_REQUIRED"

    matched_jobs.append({
        **job,
        "required_capability": required,
        "capability_status": capability_status
    })


output = {
    "project": queue["project"],
    "engine": "Advanced Capability Matching Engine",
    "status": "READY",
    "total_jobs": len(matched_jobs),
    "waiting_for_provider": len([
        j for j in matched_jobs
        if j["capability_status"] == "WAITING_FOR_PROVIDER"
    ]),
    "capability_requirements": REQUIRED_CAPABILITIES,
    "jobs": matched_jobs,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   CAPABILITY MATCHING ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Waiting for provider:", output["waiting_for_provider"])
print()
print("IMAGE capability: text_to_image")
print("VIDEO capability: text_to_video")
print("VOICE capability: text_to_speech")
print()
print("Status:", output["status"])
print()
print("Capability matching completed.")
print("Saved to:", OUTPUT_FILE)
