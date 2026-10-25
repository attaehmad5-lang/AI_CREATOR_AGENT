import json
from pathlib import Path
from datetime import datetime

# Resolve project paths from this module instead of an Android-only absolute path.
GEN = Path(__file__).resolve().parent

FILES = {
    "pipeline": GEN / "generation_pipeline_validation.json",
    "controller": GEN / "master_generation_controller_state.json",
    "connection": GEN / "provider_connection_manager_state.json",
    "capability_gate": GEN / "provider_capability_gate.json",
    "orchestration": GEN / "orchestration_plan.json",
    "state": GEN / "generation_state.json",
    "assets": GEN / "asset_manifest.json",
}


def main():
    loaded = {}
    file_status = []

    for name, path in FILES.items():
        if not path.exists():
            file_status.append({"component": name, "exists": False, "valid": False})
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded[name] = json.load(handle)
            file_status.append({"component": name, "exists": True, "valid": True})
        except (OSError, json.JSONDecodeError):
            file_status.append({"component": name, "exists": True, "valid": False})

    pipeline_ok = loaded.get("pipeline", {}).get("pipeline_integrity", False) is True
    providers_connected = loaded.get("connection", {}).get("providers_connected", 0)
    capabilities_ready = loaded.get("capability_gate", {}).get("all_capabilities_ready", False) is True
    total_jobs = loaded.get("orchestration", {}).get("total_jobs", 0)
    waiting_provider = loaded.get("orchestration", {}).get("waiting_for_provider", 0)
    waiting_dependency = loaded.get("orchestration", {}).get("waiting_for_dependency", 0)
    assets_total = loaded.get("assets", {}).get("total_assets", 0)

    if not pipeline_ok:
        health = "BLOCKED"
    elif not all(item["valid"] for item in file_status) or not capabilities_ready:
        health = "DEGRADED"
    else:
        health = "HEALTHY"

    result = {
        "system": "AI_CREATOR_AGENT",
        "component": "MASTER_SYSTEM_HEALTH",
        "health": health,
        "pipeline_integrity": pipeline_ok,
        "providers_connected": providers_connected,
        "capabilities_ready": capabilities_ready,
        "total_jobs": total_jobs,
        "waiting_for_provider": waiting_provider,
        "waiting_for_dependency": waiting_dependency,
        "assets_total": assets_total,
        "real_generation_started": False,
        "safe_mode": True,
        "real_provider_call": False,
        "file_checks": file_status,
        "status": health,
        "created_at": datetime.now().isoformat(),
    }

    output = GEN / "master_system_health.json"
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("===================================")
    print(" MASTER SYSTEM HEALTH")
    print("===================================")
    print("System Health:", health)
    print("Pipeline Integrity:", pipeline_ok)
    print("Providers Connected:", providers_connected)
    print("Capabilities Ready:", capabilities_ready)
    print("Total Jobs:", total_jobs)
    print("Waiting For Provider:", waiting_provider)
    print("Waiting For Dependency:", waiting_dependency)
    print("Assets:", assets_total)
    print("Real Generation Started:", result["real_generation_started"])
    print("Safe Mode:", result["safe_mode"])
    print("Output:", output)
    print("STATUS:", result["status"])
    return result


if __name__ == "__main__":
    main()
