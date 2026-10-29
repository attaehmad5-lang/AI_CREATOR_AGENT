"""Safe provider connection inventory.

This module detects configured credentials only. It does not make network calls,
so a detected API key must never be reported as a live provider connection.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

GEN = Path(__file__).resolve().parent
OUTPUT = GEN / "provider_connection_manager_state.json"

PROVIDERS = {
    "IMAGE": {"name": "IMAGE_PROVIDER", "env_key": "AI_IMAGE_API_KEY", "required": True},
    "VIDEO": {"name": "VIDEO_PROVIDER", "env_key": "AI_VIDEO_API_KEY", "required": True},
    "VOICE": {"name": "VOICE_PROVIDER", "env_key": "AI_VOICE_API_KEY", "required": True},
}


def key_detected(env_key):
    value = os.getenv(env_key)
    return isinstance(value, str) and bool(value.strip())


def build_connection(media_type, config):
    present = key_detected(config["env_key"])
    return {
        "media_type": media_type,
        "provider": config["name"],
        "environment_variable": config["env_key"],
        "api_key_present": present,
        "connected": False,
        "connection_state": "KEY_DETECTED" if present else "WAITING_FOR_API_KEY",
        "real_provider_call": False,
    }


def build_result():
    connections = [
        build_connection(media_type, config)
        for media_type, config in PROVIDERS.items()
    ]
    key_count = sum(item["api_key_present"] for item in connections)
    # Credentials alone do not prove connectivity; live checks are intentionally disabled.
    connected_count = sum(item["connected"] for item in connections)
    all_keys_present = all(item["api_key_present"] for item in connections)
    status = "DEGRADED" if all_keys_present else "BLOCKED"
    return {
        "manager": "PROVIDER_CONNECTION_MANAGER",
        "version": "2.1",
        "strategy": "ENVIRONMENT_VARIABLE_PROVIDER_CONNECTION",
        "providers_total": len(connections),
        "api_keys_detected": key_count,
        "providers_connected": connected_count,
        "api_keys_in_source_code": False,
        "connections": connections,
        "safe_mode": True,
        "real_provider_call": False,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    result = build_result()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
    print("===================================")
    print(" PROVIDER CONNECTION MANAGER v2.1")
    print("===================================")
    print("Providers:", result["providers_total"])
    print("API Keys Detected:", result["api_keys_detected"])
    print("Providers Connected:", result["providers_connected"])
    print("Keys In Source Code:", False)
    print("Safe Mode:", True)
    print("Real Provider Call:", False)
    print("STATUS:", result["status"])
    return result


if __name__ == "__main__":
    main()
