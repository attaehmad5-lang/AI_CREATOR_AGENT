import json
import os
from datetime import datetime

INPUT_FILE = "projects/generation/asset_manifest.json"
OUTPUT_FILE = "projects/generation/dry_run_report.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

results = []

for asset in data["assets"]:

    if asset["media_type"] == "IMAGE":
        simulated_file = asset["planned_output_path"]
    elif asset["media_type"] == "VIDEO":
        simulated_file = asset["planned_output_path"]
    elif asset["media_type"] == "VOICE":
        simulated_file = asset["planned_output_path"]
    else:
        simulated_file = asset["planned_output_path"]

    results.append({
        "asset_id": asset["asset_id"],
        "job_id": asset["job_id"],
        "scene": asset["scene"],
        "media_type": asset["media_type"],
        "simulation_status": "SIMULATED",
        "provider": "DRY_RUN_PROVIDER",
        "simulated_output": simulated_file,
        "attempts": 1,
        "message": "Generation workflow simulated successfully. No real AI provider was called.",
        "created_at": datetime.now().isoformat()
    })

output = {
    "project": data["project"],
    "engine": "Advanced Dry-Run Generation Simulator",
    "version": "1.0",
    "mode": "SAFE_SIMULATION",
    "status": "PASSED",
    "total_assets": len(results),
    "simulated_images": len([
        r for r in results if r["media_type"] == "IMAGE"
    ]),
    "simulated_videos": len([
        r for r in results if r["media_type"] == "VIDEO"
    ]),
    "simulated_voices": len([
        r for r in results if r["media_type"] == "VOICE"
    ]),
    "failed": 0,
    "real_provider_called": False,
    "results": results,
    "created_at": datetime.now().isoformat()
}

os.makedirs("projects/generation", exist_ok=True)

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   DRY-RUN GENERATION SIMULATOR")
print("===================================")
print()
print("Project:", output["project"])
print("Total assets:", output["total_assets"])
print("Simulated images:", output["simulated_images"])
print("Simulated videos:", output["simulated_videos"])
print("Simulated voices:", output["simulated_voices"])
print("Failed:", output["failed"])
print("Real provider called:", output["real_provider_called"])
print()
print("Mode:", output["mode"])
print("Status:", output["status"])
print()
print("Dry-run report created successfully.")
print("Saved to:", OUTPUT_FILE)
