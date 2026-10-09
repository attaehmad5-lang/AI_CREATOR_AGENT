import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
INPUT = BASE / "projects/generation/routed_generation_queue.json"
OUTPUT = BASE / "projects/generation/scheduled_generation_queue.json"

with open(INPUT, "r", encoding="utf-8") as f:
    data = json.load(f)

jobs = data.get("routed_jobs", [])

priority_map = {
    "VOICE": 1,
    "IMAGE": 2,
    "VIDEO": 3
}

scheduled = []

for index, job in enumerate(jobs, start=1):
    media_type = job.get("media_type", job.get("type", "UNKNOWN"))
    priority = priority_map.get(media_type, 9)

    scheduled.append({
        "schedule_position": index,
        "priority": priority,
        "job_id": job.get("job_id"),
        "scene": job.get("scene"),
        "media_type": media_type,
        "title": job.get("title"),
        "provider": job.get("provider"),
        "status": job.get("status"),
        "attempts": job.get("attempts", 0),
        "max_attempts": job.get("max_attempts", 3),
        "scheduled_at": datetime.now().isoformat()
    })

scheduled.sort(key=lambda x: (x["priority"], x["schedule_position"]))

for i, job in enumerate(scheduled, start=1):
    job["schedule_position"] = i

result = {
    "scheduler": "ADVANCED_JOB_SCHEDULER",
    "strategy": "PRIORITY_BASED",
    "total_jobs": len(scheduled),
    "priority_rules": priority_map,
    "scheduled_jobs": scheduled,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" ADVANCED JOB SCHEDULER")
print("===================================")
print("Total Jobs:", len(scheduled))
print("Priority Based: READY")
print("Queue Scheduled:", len(scheduled))
print("Output:", OUTPUT)
print("STATUS: READY")
