import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/generation_jobs.json"
OUTPUT_FILE = "projects/generation/generation_queue.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

queue = []

for index, job in enumerate(data["jobs"], start=1):
    queue.append({
        "queue_position": index,
        "job_id": job["job_id"],
        "type": job["type"],
        "scene": job["scene"],
        "title": job["title"],
        "provider": job["provider"],
        "status": "QUEUED",
        "attempts": 0,
        "max_attempts": 3,
        "created_at": datetime.now().isoformat()
    })

output = {
    "project": data["project"],
    "status": "READY",
    "engine": "Generation Queue Engine",
    "queue_size": len(queue),
    "queued": len(queue),
    "processing": 0,
    "completed": 0,
    "failed": 0,
    "queue": queue,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("       GENERATION QUEUE ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Queue size:", output["queue_size"])
print("Queued:", output["queued"])
print("Processing:", output["processing"])
print("Completed:", output["completed"])
print("Failed:", output["failed"])
print()
print("Status:", output["status"])
print()
print("Generation queue created successfully.")
print("Saved to:", OUTPUT_FILE)
