import json
import os
from pathlib import Path
from datetime import datetime

BASE = Path("/storage/emulated/0/AI_CREATOR_AGENT")
GEN = BASE / "projects/generation"
OUTPUT = GEN / "provider_connection_manager_state.json"

PROVIDERS = {
    "IMAGE": {
        "name": "IMAGE_PROVIDER",
        "env_key": "AI_IMAGE_API_KEY",
        "required": True
    },
    "VIDEO": {
        "name": "VIDEO_PROVIDER",
        "env_key": "AI_VIDEO_API_KEY",
        "required": True
    },
    "VOICE": {
        "name": "VOICE_PROVIDER",
        "env_key": "AI_VOICE_API_KEY",
        "required": True
    }
}

def key_detected(env_key):
    value = os.getenv(env_key)
    return bool(value and value.strip())

def build_connection(media_type, config):
    present = key_detected(config["env_key"])

    if present:
        state = "KEY_DETECTED"
    else:
        state = "WAITING_FOR_API_KEY"

    return {
        "media_type": media_type,
        "provider": config["name"],
        "environment_variable": config["env_key"],
        "api_key_present": present,
        "connected": False,
        "connection_state": state,
        "real_provider_call": False
    }

connections = [
    build_connection(media_type, config)
    for media_type, config in PROVIDERS.items()
]

key_count = sum(1 for item in connections if item["api_key_present"])
connected_count = sum(1 for item in connections if item["connected"])

result = {
    "manager": "PROVIDER_CONNECTION_MANAGER",
    "version": "2.0",
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

GEN.mkdir(parents=True, exist_ok=True)

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print("===================================")
print(" PROVIDER CONNECTION MANAGER v2.0")
print("===================================")
print("Providers:", len(connections))
print("API Keys Detected:", key_count)
print("Providers Connected:", connected_count)
print("Keys In Source Code:", False)
print("Safe Mode:", True)
print("Real Provider Call:", False)
print("STATUS: READY")
