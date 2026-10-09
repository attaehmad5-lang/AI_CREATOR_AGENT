import json
import os
from datetime import datetime

QUEUE_FILE = "projects/generation/routed_generation_queue.json"
REGISTRY_FILE = "projects/generation/provider_registry.json"
GATE_FILE = "projects/generation/provider_capability_gate.json"
OUTPUT_FILE = "projects/generation/capability_matched_queue.json"

with open(QUEUE_FILE, "r", encoding="utf-8") as f:
    queue = json.load(f)

with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
    registry = json.load(f)

with open(GATE_FILE, "r", encoding="utf-8") as f:
    gate = json.load(f)


REQUIRED_CAPABILITIES = {
    "IMAGE": ["text_to_image"],
    "VIDEO": ["text_to_video", "image_to_video"],
    "VOICE": ["text_to_speech"]
}


def get_capability(job_type):
    return REQUIRED_CAPABILITIES.get(job_type)


matched_jobs = []

for job in queue["routed_jobs"]:

    required = get_capability(job["type"])

    gate_check = next((x for x in gate.get("checks", []) if x.get("media_type") == job["type"]), {})
    if job["provider"] in (None, "NOT_CONNECTED"):
        capability_status = "WAITING_FOR_PROVIDER"
    elif not gate_check.get("gate_pass", False):
        capability_status = "CAPABILITY_BLOCKED"
    else:
        capability_status = "CAPABILITY_MATCHED"

    matched_jobs.append({
        **job,
        "required_capability": required,
        "capability_status": capability_status
    })


status_counts = {x: sum(1 for j in matched_jobs if j.get("capability_status") == x) for x in ["CAPABILITY_MATCHED", "CAPABILITY_BLOCKED", "WAITING_FOR_PROVIDER"]}
if status_counts["CAPABILITY_BLOCKED"] == len(matched_jobs):
    overall_status = "BLOCKED"
elif status_counts["CAPABILITY_MATCHED"] == len(matched_jobs):
    overall_status = "READY"
elif status_counts["CAPABILITY_MATCHED"] > 0:
    overall_status = "DEGRADED"
elif status_counts["CAPABILITY_BLOCKED"] > 0:
    overall_status = "DEGRADED"
else:
    overall_status = "WAITING_FOR_PROVIDER"

output = {
    "project": queue["project"],
    "engine": "Advanced Capability Matching Engine",
    "status": overall_status,
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
