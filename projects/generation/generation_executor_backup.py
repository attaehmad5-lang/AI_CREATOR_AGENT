import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/capability_matched_queue.json"
OUTPUT_FILE = "projects/generation/execution_report.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

execution_jobs = []

for job in data["jobs"]:

    if job["capability_status"] == "WAITING_FOR_PROVIDER":
        status = "WAITING_FOR_PROVIDER"
        message = "No compatible AI provider is connected yet."
    else:
        status = "READY_FOR_EXECUTION"
        message = "Job can be sent to a compatible provider."

    execution_jobs.append({
        "job_id": job["job_id"],
        "type": job["type"],
        "scene": job["scene"],
        "required_capability": job["required_capability"],
        "selected_provider": job["selected_provider"],
        "status": status,
        "message": message,
        "attempts": 0,
        "max_attempts": 3,
        "created_at": datetime.now().isoformat()
    })


output = {
    "project": data["project"],
    "engine": "Advanced Generation Execution Engine",
    "status": "READY",
    "total_jobs": len(execution_jobs),
    "waiting_for_provider": len([
        j for j in execution_jobs
        if j["status"] == "WAITING_FOR_PROVIDER"
    ]),
    "ready_for_execution": len([
        j for j in execution_jobs
        if j["status"] == "READY_FOR_EXECUTION"
    ]),
    "completed": 0,
    "failed": 0,
    "jobs": execution_jobs,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   GENERATION EXECUTION ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Waiting for provider:", output["waiting_for_provider"])
print("Ready for execution:", output["ready_for_execution"])
print("Completed:", output["completed"])
print("Failed:", output["failed"])
print()
print("Status:", output["status"])
print()
print("Execution report created successfully.")
print("Saved to:", OUTPUT_FILE)
