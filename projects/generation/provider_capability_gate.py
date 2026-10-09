import json
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
GEN = BASE / "projects/generation"

CONNECTION = GEN / "provider_connection_manager_state.json"
REGISTRY = GEN / "provider_registry.json"

OUTPUT = GEN / "provider_capability_gate.json"

with open(CONNECTION, "r", encoding="utf-8") as f:
    connection_data = json.load(f)

with open(REGISTRY, "r", encoding="utf-8") as f:
    registry_data = json.load(f)

required = {
    "IMAGE": ["text_to_image"],
    "VIDEO": ["text_to_video", "image_to_video"],
    "VOICE": ["text_to_speech"]
}

capabilities = {"IMAGE": registry_data.get("image", {}).get("capabilities", []), "VIDEO": registry_data.get("video", {}).get("capabilities", []), "VOICE": registry_data.get("voice", {}).get("capabilities", [])}

checks = []

for media_type, required_capabilities in required.items():

    provider_connection = next(
        (
            x for x in connection_data.get("connections", [])
            if x.get("media_type") == media_type
        ),
        {}
    )

    provider_connected = provider_connection.get(
        "connected", False
    )

    provider_capabilities = capabilities.get(
        media_type, []
    )

    capability_available = (
        all(x in provider_capabilities for x in required_capabilities)
    )

    gate_pass = (
        provider_connected and capability_available
    )

    checks.append({
        "media_type": media_type,
        "required_capabilities": required_capabilities,
        "provider_connected": provider_connected,
        "capability_available": capability_available,
        "gate_pass": gate_pass,
        "gate_state": (
            "READY"
            if gate_pass
            else "BLOCKED"
        )
    })

passed = sum(
    1 for x in checks if x["gate_pass"]
)

blocked = sum(
    1 for x in checks if not x["gate_pass"]
)

result = {
    "gate": "PROVIDER_CAPABILITY_GATE",
    "strategy": "CONNECTION_AND_CAPABILITY_VALIDATION",
    "total_checks": len(checks),
    "passed": passed,
    "blocked": blocked,
    "all_capabilities_ready": blocked == 0,
    "real_provider_call": False,
    "safe_mode": True,
    "checks": checks,
    "status": ("READY" if blocked == 0 else ("DEGRADED" if passed > 0 else "BLOCKED")),
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" PROVIDER CAPABILITY GATE")
print("===================================")
print("Total Checks:", result["total_checks"])
print("Passed:", result["passed"])
print("Blocked:", result["blocked"])
print("All Capabilities Ready:", result["all_capabilities_ready"])
print("Real Provider Call:", False)
print("Safe Mode:", True)
print("Output:", OUTPUT)
print("STATUS:", result["status"])
