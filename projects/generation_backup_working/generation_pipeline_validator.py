import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
GEN = BASE / "projects/generation"

FILES = {
    "adapter_status": GEN / "generation_adapter_status.json",
    "jobs": GEN / "generation_jobs.json",
    "queue": GEN / "generation_queue.json",
    "provider_config": GEN / "provider_config.json",
    "provider_health": GEN / "provider_health.json",
    "provider_registry": GEN / "provider_registry.json",
    "routing": GEN / "provider_routing.json",
    "capability_queue": GEN / "capability_matched_queue.json",
    "execution_report": GEN / "execution_report.json",
    "asset_manifest": GEN / "asset_manifest.json",
    "dry_run": GEN / "dry_run_report.json",
    "generation_state": GEN / "generation_state.json",
    "retry_state": GEN / "retry_state.json",
    "provider_connection": GEN / "provider_connection.json",
    "provider_capability_router": GEN / "provider_routing.json",
    "scheduled_queue": GEN / "scheduled_generation_queue.json",
    "dependencies": GEN / "job_dependencies.json",
    "dependency_plan": GEN / "dependency_execution_plan.json",
    "orchestration": GEN / "orchestration_plan.json"
}

results = []
passed = 0
failed = 0

for name, path in FILES.items():
    if not path.exists():
        results.append({
            "check": name,
            "status": "FAILED",
            "reason": "FILE_NOT_FOUND"
        })
        failed += 1
        continue

    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)

        results.append({
            "check": name,
            "status": "PASSED",
            "reason": "VALID_JSON"
        })
        passed += 1

    except Exception as e:
        results.append({
            "check": name,
            "status": "FAILED",
            "reason": str(e)
        })
        failed += 1

result = {
    "validator": "GENERATION_PIPELINE_VALIDATOR",
    "total_checks": len(FILES),
    "passed": passed,
    "failed": failed,
    "pipeline_integrity": failed == 0,
    "real_generation_started": False,
    "status": "PASSED" if failed == 0 else "FAILED",
    "checks": results,
    "created_at": datetime.now().isoformat()
}

OUTPUT = GEN / "generation_pipeline_validation.json"

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" GENERATION PIPELINE VALIDATOR")
print("===================================")
print("Total Checks:", result["total_checks"])
print("Passed:", result["passed"])
print("Failed:", result
["failed"])
print("Pipeline Integrity:", result["pipeline_integrity"])
print("Real Generation Started:", result["real_generation_started"])
print("Output:", OUTPUT)
print("STATUS:", result["status"])
