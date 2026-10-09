import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
INPUT = BASE / "projects/generation/scheduled_generation_queue.json"
OUTPUT = BASE / "projects/generation/job_dependencies.json"

with open(INPUT, "r", encoding="utf-8") as f:
    data = json.load(f)

jobs = data.get("scheduled_jobs", [])

dependencies = []

for job in jobs:
    media_type = job.get("media_type")
    job_id = job.get("job_id")
    scene = job.get("scene")

    depends_on = []

    if media_type == "VIDEO":
        depends_on = [
            f"{scene}_IMAGE",
            f"{scene}_VOICE"
        ]

    elif media_type == "VOICE":
        depends_on = []

    elif media_type == "IMAGE":
        depends_on = []

    dependencies.append({
        "job_id": job_id,
        "scene": scene,
        "media_type": media_type,
        "depends_on": depends_on,
        "dependency_count": len(depends_on),
        "dependency_status": "WAITING" if depends_on else "READY"
    })

result = {
    "manager": "JOB_DEPENDENCY_MANAGER",
    "strategy": "SCENE_MEDIA_DEPENDENCY",
    "total_jobs": len(dependencies),
    "jobs_with_dependencies": sum(
        1 for x in dependencies if x["dependency_count"] > 0
    ),
    "independent_jobs": sum(
        1 for x in dependencies if x["dependency_count"] == 0
    ),
    "dependencies": dependencies,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" JOB DEPENDENCY MANAGER")
print("===================================")
print("Total Jobs:", len(dependencies))
print("Jobs With Dependencies:", result["jobs_with_dependencies"])
print("Independent Jobs:", result["independent_jobs"])
print("Output:", OUTPUT)
print("STATUS: READY")
