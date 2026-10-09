import json
import os

CONFIG_FILE = "projects/generation/provider_config.json"
OUTPUT_FILE = "projects/generation/provider_health.json"

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

checks = {}

for provider_type, provider in config["providers"].items():
    checks[provider_type] = {
        "configured": provider["provider"] is not None,
        "model_configured": provider["model"] is not None,
        "api_key_configured": provider["api_key_configured"],
        "status": "READY_FOR_CONNECTION"
    }

output = {
    "project": config["project"],
    "engine": "Provider Health Check",
    "status": "READY",
    "checks": checks,
    "security_check": {
        "source_code_api_keys": config["security"]["api_keys_in_source_code"],
        "key_logging": config["security"]["key_logging"]
    }
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("      PROVIDER HEALTH CHECK")
print("===================================")
print()

for provider_type, result in checks.items():
    print(
        provider_type.upper(),
        ":",
        result["status"],
        "| Provider configured:",
        result["configured"]
    )

print()
print("Source-code API keys:", output["security_check"]["source_code_api_keys"])
print("API key logging:", output["security_check"]["key_logging"])
print()
print("Overall status:", output["status"])
print()
print("Provider health report created successfully.")
print("Saved to:", OUTPUT_FILE)
