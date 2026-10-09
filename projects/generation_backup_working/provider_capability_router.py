import json
import os
from datetime import datetime

QUEUE_FILE = "projects/generation/retry_state.json"
PROVIDER_FILE = "projects/generation/provider_connection.json"
OUTPUT_FILE = "projects/generation/provider_routing.json"

with open(QUEUE_FILE, "r", encoding="utf-8") as f:
    queue_data = json.load(f)

with open(PROVIDER_FILE, "r", encoding="utf-8") as f:
    provider_data = json.load(f)

capability_map = {
    "IMAGE": "text_to_image",
    "VIDEO": "text_to_video",
    "VOICE": "text_to_speech"
}

routed_jobs = []

for job in queue_data["jobs"]:

    media_type = job["media_type"]
    required_capability = capability_map.get(
        media_type,
        "unknown"
    )

    matching_providers = []

    for provider in provider_data["providers"]:
        if (
            provider["type"] == media_type
            and required_capability in provider["capabilities"]
            and provider["connected"]
        ):
            matching_providers.append(provider["provider_id"])

    if matching_providers:
        selected_provider = matching_providers[0]
        route_status = "READY_FOR_PROVIDER"
        next_action = "SEND_TO_PROVIDER"
    else:
        selected_provider = None
        route_status = "WAITING_FOR_PROVIDER"
        next_action = "WAIT_FOR_COMPATIBLE_PROVIDER"

    routed_jobs.append({
        "asset_id": job["asset_id"],
        "job_id": job["job_id"],
        "scene": job["scene"],
        "media_type": media_type,
        "required_capability": required_capability,
        "selected_provider": selected_provider,
        "route_status": route_status,
        "next_action": next_action,
        "attempts": job["attempts"],
        "max_attempts": job["max_attempts"],
        "updated_at": datetime.now().isoformat()
    })

output = {
    "project": queue_data["project"],
    "engine": "Advanced Provider Capability Router",
    "version": "1.0",
    "status": "READY",

    "total_jobs": len(routed_jobs),

    "ready_for_provider": len([
        j for j in routed_jobs
        if j["route_status"] == "READY_FOR_PROVIDER"
    ]),

    "waiting_for_provider": len([
        j for j in routed_jobs
        if j["route_status"] == "WAITING_FOR_PROVIDER"
    ]),

    "send_to_provider": len([
        j for j in routed_jobs
        if j["next_action"] == "SEND_TO_PROVIDER"
    ]),

    "jobs": routed_jobs,

    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   PROVIDER CAPABILITY ROUTER")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Ready for provider:", output["ready_for_provider"])
print("Waiting for provider:", output["waiting_for_provider"])
print("Send to provider:", output["send_to_provider"])
print()
print("Status:", output["status"])
print()
print("Provider routing report created successfully.")
print("Saved to:", OUTPUT_FILE)
