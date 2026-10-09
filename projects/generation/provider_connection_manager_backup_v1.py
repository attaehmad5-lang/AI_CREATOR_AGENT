import json
import os
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
GEN = BASE / "projects/generation"

OUTPUT = GEN / "provider_connection_manager_state.json"

providers = {
    "IMAGE": {
        "name": "IMAGE_PROVIDER",
        "env_key": "AI_IMAGE_API_KEY"
    },
    "VIDEO": {
        "name": "VIDEO_PROVIDER",
        "env_key": "AI_VIDEO_API_KEY"
    },
    "VOICE": {
        "name": "VOICE_PROVIDER",
        "env_key": "AI_VOICE_API_KEY"
    }
}

connections = []

for media_type, config in providers.items():
    api_key_present = bool(os.getenv(config["env_key"]))

    connections.append({
        "media_type": media_type,
        "provider": config["name"],
        "environment_variable": config["env_key"],
        "api_key_present": api_key_present,
        "connected": False,
        "connection_state": (
            "KEY_DETECTED"
            if api_key_present
            else "WAITING_FOR_API_KEY"
        )
    })

connected_count = sum(
    1 for x in connections if x["connected"]
)

key_count = sum(
    1 for x in connections if x["api_key_present"]
)

result = {
    "manager": "PROVIDER_CONNECTION_MANAGER",
    "strategy": "ENVIRONMENT_VARIABLE_PROVIDER_CONNECTION",
    "providers_total": len(connections),
    "api_keys_detected": key_count,
    "providers_connected": connected_count,
    "api_keys_in_source_code": False,
    "connections": connections,
    "safe_mode": True,
    "real_provider_call": False,
    "status": "READY",
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print()
print("===================================")
print(" PROVIDER CONNECTION MANAGER")
print("===================================")
print("Providers:", len(connections))
print("API Keys Detected:", key_count)
print("Providers Connected:", connected_count)
print("Keys In Source Code:", False)
print("Safe Mode:", True)
print("Real Provider Call:", False)
print("Output:", OUTPUT)
print("STATUS: READY")
