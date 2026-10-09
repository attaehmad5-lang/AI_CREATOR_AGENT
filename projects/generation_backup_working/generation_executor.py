import json
import os
from datetime import datetime

from agents.generation.generation_adapter import GenerationAdapter

INPUT_FILE = "projects/generation/orchestration_plan.json"
JOBS_FILE = "projects/generation/generation_jobs.json"
OUTPUT_FILE = "projects/generation/execution_report.json"
OUTPUT_DIR = "projects/generation"


with open(INPUT_FILE, "r", encoding="utf-8") as f:
    capability_data = json.load(f)

with open(JOBS_FILE, "r", encoding="utf-8") as f:
    generation_data = json.load(f)


job_lookup = {
    job["job_id"]: job
    for job in generation_data.get("jobs", [])
}

adapter = GenerationAdapter()
execution_jobs = []


for job in capability_data.get("jobs", []):

    job_id = job["job_id"]
    job_type = job.get("media_type", job.get("type"))

    source_job = job_lookup.get(job_id, {})

    result = {
        "job_id": job_id,
        "type": job_type,
        "scene": job.get("scene"),
        "title": job.get("title", source_job.get("title", "")),
        "selected_provider": job.get("selected_provider"),
        "status": "WAITING_FOR_PROVIDER",
        "message": "No compatible provider is ready.",
        "attempts": 0,
        "max_attempts": 3,
        "created_at": datetime.now().isoformat()
    }

    if job.get("can_execute") is True and job_type == "IMAGE":

        prompt = source_job.get("prompt", "")

        if not prompt:
            result["status"] = "FAILED"
            result["message"] = "Image prompt is missing."
        else:
            output_file = os.path.join(
                OUTPUT_DIR,
                f"{job_id}.png"
            )

            if os.path.exists(output_file):
                result["status"] = "COMPLETED"
                result["message"] = "Existing generated image reused."
                result["output"] = output_file
                result["provider"] = "Hugging Face"
            else:
                try:
                    result["attempts"] = 1

                    generated = adapter.generate_image(
                        prompt,
                        output_file
                    )

                    result["status"] = generated.get("status", "COMPLETED") if isinstance(generated, dict) else "COMPLETED"
                    result["message"] = ("Image generated successfully." if result["status"] == "COMPLETED" else str(generated.get("errors", generated.get("message", "Generation failed"))))
                    result["output"] = (generated.get("output") or generated.get("file_path") or generated.get("image_path") or output_file) if isinstance(generated, dict) else output_file
                    result["provider"] = generated.get("provider") if isinstance(generated, dict) else "Hugging Face"

                except Exception as e:
                    result["status"] = "FAILED"
                    result["message"] = str(e)

    execution_jobs.append(result)


completed = len([
    j for j in execution_jobs
    if j["status"] == "COMPLETED"
])

failed = len([
    j for j in execution_jobs
    if j["status"] == "FAILED"
])

waiting = len([
    j for j in execution_jobs
    if j["status"] == "WAITING_FOR_PROVIDER"
])

ready = len([
    j for j in execution_jobs
    if j["status"] == "READY_FOR_EXECUTION"
])


output = {
    "project": capability_data.get("project", capability_data.get("orchestrator", "AI_CREATOR_AGENT")),
    "engine": "Advanced Generation Execution Engine",
    "status": "READY",
    "total_jobs": len(execution_jobs),
    "waiting_for_provider": waiting,
    "ready_for_execution": ready,
    "completed": completed,
    "failed": failed,
    "jobs": execution_jobs,
    "created_at": datetime.now().isoformat()
}


os.makedirs(OUTPUT_DIR, exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)


print("===================================")
print("   GENERATION EXECUTION ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Total jobs:", output["total_jobs"])
print("Waiting for provider:", output["waiting_for_provider"])
print("Completed:", output["completed"])
print("Failed:", output["failed"])
print()
print("Status:", output["status"])
print()
print("Execution report created successfully.")
print("Saved to:", OUTPUT_FILE)
