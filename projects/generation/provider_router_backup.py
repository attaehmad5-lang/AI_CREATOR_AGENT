import json
import os
from datetime import datetime

REGISTRY_FILE = "projects/generation/provider_registry.json"
QUEUE_FILE = "projects/generation/generation_queue.json"
OUTPUT_FILE = "projects/generation/routed_generation_queue.json"

with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
    registry = json.load(f)

with open(QUEUE_FILE, "r", encoding="utf-8") as f:
    queue_data = json.load(f)


def get_route(job_type):
    key = job_type.lower()

    if key == "image":
        config = registry["image"]
    elif key == "video":
        config = registry["video"]
    elif key == "voice":
        config = registry["voice"]
    else:
        return {
            "provider": None,
            "route_status": "UNSUPPORTED_TYPE"
        }

    if config["primary"]:
        return {
            "provider": config["primary"],
            "route_status": "PRIMARY"
        }

    if config["fallbacks"]:
        return {
            "provider": config["fallbacks"][0],
            "route_status": "FALLBACK"
        }

    return {
        "provider": None,
        "route_status": "WAITING_FOR_PROVIDER"
    }


routed_jobs = []

for job in queue_data["queue"]:
    route = get_route(job["type"])

    routed_jobs.append({
        **job,
        "selected_provider": route["provider"],
        "route_status": route["route_status"],
        "routing_timestamp": datetime.now().isoformat()
    })


output = {
    "project": queue_data["project"],
    "engine": "Advanced Provider Router",
    "status": "READY",
    "routing_strategy": registry["routing"]["strategy"],
    "automatic_fallback": registry["routing"]["automatic_fallback"],
    "total_jobs": len(routed_jobs),
    "waiting_for_provider": len([
        j for j in routed_jobs
        if j["route_status"] == "WAITING_FOR_PROVIDER"
    ]),
    "routed_jobs": routed_jobs,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("      ADVANCED PROVIDER ROUTER")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Waiting for provider:", output["waiting_for_provider"])
print("Routing strategy:", output["routing_strategy"])
print("Automatic fallback:", output["automatic_fallback"])
print()
print("Status:", output["status"])
print()
print("Provider routing completed.")
print("Saved to:", OUTPUT_FILE)
