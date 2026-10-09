import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")

SCHEDULE = BASE / "projects/generation/scheduled_generation_queue.json"
DEPENDENCIES = BASE / "projects/generation/dependency_execution_plan.json"
ROUTING = BASE / "projects/generation/provider_routing.json"
OUTPUT = BASE / "projects/generation/orchestration_plan.json"

with open(SCHEDULE, "r", encoding="utf-8") as f:
    schedule_data = json.load(f)

with open(DEPENDENCIES, "r", encoding="utf-8") as f:
    dependency_data = json.load(f)

with open(ROUTING, "r", encoding="utf-8") as f:
    routing_data = json.load(f)

scheduled_jobs = schedule_data.get("scheduled_jobs", [])
dependency_jobs = dependency_data.get("execution_plan", [])
routing_jobs = routing_data.get("jobs", [])

dependency_map = {
    job.get("job_id"): job
    for job in dependency_jobs
}

routing_map = {
    job.get("job_id"): job
    for job in routing_jobs
}

orchestration = []

for job in scheduled_jobs:
    job_id = job.get("job_id")

    dependency = dependency_map.get(job_id, {})
    routing = routing_map.get(job_id, {})

    can_execute = dependency.get("can_execute", False)
    provider_status = routing.get("status", "WAITING_FOR_PROVIDER")

    if can_execute and provider_status == "READY":
        state = "READY_FOR_EXECUTION"
    elif not can_execute:
        state = "WAITING_FOR_DEPENDENCY"
    else:
        state = "WAITING_FOR_PROVIDER"

    orchestration.append({
        "schedule_position": job.get("schedule_position"),
        "job_id": job_id,
        "scene": job.get("scene"),
        "media_type": job.get("media_type"),
        "priority": job.get("priority"),
        "provider": job.get("provider"),
        "dependencies": dependency.get("depends_on", []),
        "provider_status": provider_status,
        "orchestration_state": state,
        "can_execute": state == "READY_FOR_EXECUTION"
    })

result = {
    "orchestrator": "GENERATION_ORCHESTRATOR",
    "strategy": "CENTRALIZED_GENERATION_CONTROL",
    "total_jobs": len(orchestration),
    "ready_for_execution": sum(
        1 for x in orchestration
        if x["can_execute"]
    ),
    "waiting_for_dependency": sum(
        1 for x in orchestration
        if x["orchestration_state"] == "WAITING_FOR_DEPENDENCY"
    ),
    "waiting_for_provider": sum(
        1 for x in orchestration
        if x["orchestration_state"] == "WAITING_FOR_PROVIDER"
    ),
    "jobs": orchestration,
    "real_generation_started": False,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" GENERATION ORCHESTRATOR")
print("===================================")
print("Total Jobs:", result["total_jobs"])
print("Ready For Execution:", result["ready_for_execution"])
print("Waiting For Dependency:", result["waiting_for_dependency"])
print("Waiting For Provider:", result["waiting_for_provider"])
print("Real Generation Started:", result["real_generation_started"])
print("Output:", OUTPUT)
print("STATUS: READY")
