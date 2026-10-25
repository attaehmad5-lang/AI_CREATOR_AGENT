import json
from pathlib import Path
from datetime import datetime

# Resolve paths relative to this file so the project works on Android,
# Linux CI, Windows, and local development checkouts.
GEN = Path(__file__).resolve().parent

CONNECTION = GEN / "provider_connection_manager_state.json"
REGISTRY = GEN / "provider_registry.json"
OUTPUT = GEN / "provider_capability_gate.json"


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    connection_data = load_json(CONNECTION)
    registry_data = load_json(REGISTRY)

    required = {
        "IMAGE": ["text_to_image"],
        "VIDEO": ["text_to_video", "image_to_video"],
        "VOICE": ["text_to_speech"],
    }

    capabilities = {
        "IMAGE": registry_data.get("image", {}).get("capabilities", []),
        "VIDEO": registry_data.get("video", {}).get("capabilities", []),
        "VOICE": registry_data.get("voice", {}).get("capabilities", []),
    }

    checks = []
    for media_type, required_capabilities in required.items():
        provider_connection = next(
            (
                item
                for item in connection_data.get("connections", [])
                if item.get("media_type") == media_type
            ),
            {},
        )
        provider_connected = provider_connection.get("connected", False) is True
        provider_capabilities = capabilities.get(media_type, [])
        capability_available = all(
            item in provider_capabilities for item in required_capabilities
        )
        gate_pass = provider_connected and capability_available

        checks.append({
            "media_type": media_type,
            "required_capabilities": required_capabilities,
            "provider_connected": provider_connected,
            "capability_available": capability_available,
            "gate_pass": gate_pass,
            "gate_state": "READY" if gate_pass else "BLOCKED",
        })

    passed = sum(1 for item in checks if item["gate_pass"])
    blocked = len(checks) - passed
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
        "status": "READY" if blocked == 0 else ("DEGRADED" if passed else "BLOCKED"),
        "created_at": datetime.now().isoformat(),
    }

    OUTPUT.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("===================================")
    print(" PROVIDER CAPABILITY GATE")
    print("===================================")
    print("Total Checks:", result["total_checks"])
    print("Passed:", result["passed"])
    print("Blocked:", result["blocked"])
    print("All Capabilities Ready:", result["all_capabilities_ready"])
    print("Real Provider Call:", result["real_provider_call"])
    print("Safe Mode:", result["safe_mode"])
    print("Output:", OUTPUT)
    print("STATUS:", result["status"])
    return result


if __name__ == "__main__":
    main()
