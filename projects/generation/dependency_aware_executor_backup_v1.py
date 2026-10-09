import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
INPUT = BASE / "projects/generation/job_dependencies.json"
OUTPUT = BASE / "projects/generation/dependency_execution_plan.json"

with open(INPUT, "r", encoding="utf-8") as f:
    data = json.load(f)

jobs = data.get("dependencies", [])

execution_plan = []

for job in jobs:
    dependencies = job.get("depends_on", [])

    if dependencies:
        state = "WAITING_FOR_DEPENDENCY"
    else:
        state = "READY"

    execution_plan.append({
        "job_id": job.get("job_id"),
        "scene": job.get("scene"),
        "media_type": job.get("media_type"),
        "depends_on": dependencies,
        "dependency_count": len(dependencies),
        "execution_state": state,
        "can_execute": state == "READY"
    })

result = {
    "executor": "DEPENDENCY_AWARE_EXECUTOR",
    "strategy": "DEPENDENCY_GATED_EXECUTION",
    "total_jobs": len(execution_plan),
    "ready_jobs": sum(
        1 for x in execution_plan if x["can_execute"]
    ),
    "waiting_jobs": sum(
        1 for x in execution_plan if not x["can_execute"]
    ),
    "execution_plan": execution_plan,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" DEPENDENCY-AWARE EXECUTOR")
print("===================================")
print("Total Jobs:", result["total_jobs"])
print("Ready Jobs:", result["ready_jobs"])
print("Waiting Jobs:", result["waiting_jobs"])
print("Output:", OUTPUT)
print("STATUS: READY")
