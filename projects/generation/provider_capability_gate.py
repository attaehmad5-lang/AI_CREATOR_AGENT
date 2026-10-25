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
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def main():
    required = {
        "IMAGE": ["text_to_image"],
        "VIDEO": ["text_to_video", "image_to_video"],
        "VOICE": ["text_to_speech"],
    }
    input_error = None
    try:
        connection_data = load_json(CONNECTION)
        registry_data = load_json(REGISTRY)
        if "connections" not in connection_data:
            raise ValueError("connections field is required")
        connections = connection_data["connections"]
        if not isinstance(connections, list) or not all(
            isinstance(item, dict) for item in connections
        ):
            raise ValueError("connections must be a list of objects")

        capabilities = {}
        for media_type in required:
            key = media_type.lower()
            if key not in registry_data:
                raise ValueError("provider metadata is missing")
            provider = registry_data[key]
            if not isinstance(provider, dict):
                raise ValueError("provider metadata must be an object")
            declared = provider.get("capabilities", [])
            if not isinstance(declared, list) or not all(
                isinstance(item, str) for item in declared
            ):
                raise ValueError("capabilities must be a list of strings")
            capabilities[media_type] = declared
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        # Do not expose filesystem paths or exception details in status output.
        input_error = type(exc).__name__
        connection_data = {}
        connections = []
        capabilities = {media_type: [] for media_type in required}

    checks = []
    for media_type, required_capabilities in required.items():
        provider_connection = next(
            (
                item
                for item in connections
                if item.get("media_type") == media_type
            ),
            {},
        )
        provider_connected = provider_connection.get("connected", False) is True
        provider_capabilities = capabilities.get(media_type, [])
        capability_available = all(
            item in provider_capabilities for item in required_capabilities
        )
        gate_pass = input_error is None and provider_connected and capability_available

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
    status = (
        "BLOCKED" if input_error is not None or passed == 0
        else ("READY" if blocked == 0 else "DEGRADED")
    )
    result = {
        "gate": "PROVIDER_CAPABILITY_GATE",
        "strategy": "CONNECTION_AND_CAPABILITY_VALIDATION",
        "total_checks": len(checks),
        "passed": passed,
        "blocked": blocked,
        "all_capabilities_ready": blocked == 0 and input_error is None,
        "input_valid": input_error is None,
        "input_error_type": input_error,
        "real_provider_call": False,
        "safe_mode": True,
        "checks": checks,
        "status": status,
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
