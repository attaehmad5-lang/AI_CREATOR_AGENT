import json
from pathlib import Path
from datetime import datetime
from projects.generation.providers.provider_service import ProviderService

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
GEN = BASE / "projects/generation"

FILES = {
    "pipeline_validation": GEN / "generation_pipeline_validation.json",
    "orchestration": GEN / "orchestration_plan.json",
    "state": GEN / "generation_state.json",
    "retry": GEN / "retry_state.json",
    "assets": GEN / "asset_manifest.json",
    "provider_health": GEN / "provider_health.json"
}

loaded = {}

for name, path in FILES.items():
    with open(path, "r", encoding="utf-8") as f:
        loaded[name] = json.load(f)

validation = loaded["pipeline_validation"]
orchestration = loaded["orchestration"]
state = loaded["state"]
retry = loaded["retry"]
assets = loaded["assets"]
provider_health = loaded["provider_health"]
provider_service = ProviderService()

providers_ready = bool(provider_service.manager.providers)

total_jobs = orchestration.get("total_jobs", 0)

ready_jobs = orchestration.get("ready_for_execution", 0)

executable_jobs = [j for j in orchestration.get("jobs", []) if j.get("can_execute") is True and j.get("orchestration_state") == "READY_FOR_EXECUTION"]
safe_executable_jobs = len(executable_jobs)

waiting_dependency = orchestration.get(
    "waiting_for_dependency", 0
)

waiting_provider = orchestration.get(
    "waiting_for_provider", 0
)

if not validation.get("pipeline_integrity", False):
    controller_state = "BLOCKED_PIPELINE_VALIDATION"

elif total_jobs == 0:
    controller_state = "NO_JOBS"

elif not providers_ready:
    controller_state = "WAITING_FOR_PROVIDER"

elif safe_executable_jobs > 0:
    controller_state = "READY_TO_GENERATE"

else:
    controller_state = "WAITING_FOR_DEPENDENCIES"

result = {
    "controller": "MASTER_GENERATION_CONTROLLER",
    "version": "1.0",
    "pipeline_integrity": validation.get(
        "pipeline_integrity", False
    ),
    "total_jobs": total_jobs,
    "ready_jobs": ready_jobs,
    "safe_executable_jobs": safe_executable_jobs,
    "executable_job_ids": [j.get("job_id") for j in executable_jobs],
    "waiting_for_dependency": waiting_dependency,
    "waiting_for_provider": waiting_provider,
    "providers_ready": providers_ready,
    "assets_total": assets.get("total_assets", 0),
    "assets_completed": assets.get("completed_assets", 0),
    "retry_available": retry.get("retry_available", 0),
    "generation_started": False,
    "controller_state": controller_state,
    "safe_mode": True,
    "real_provider_call": False,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

OUTPUT = GEN / "master_generation_controller_state.json"

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" MASTER GENERATION CONTROLLER")
print("===================================")
print("Total Jobs:", total_jobs)
print("Ready Jobs:", ready_jobs)
print("Waiting For Dependency:", waiting_dependency)
print("Waiting For Provider:", waiting_provider)
print("Providers Ready:", providers_ready)
print("Generation Started:", False)
print("Safe Mode:", True)
print("Controller State:", controller_state)
print("Output:", OUTPUT)
print("STATUS: READY")
