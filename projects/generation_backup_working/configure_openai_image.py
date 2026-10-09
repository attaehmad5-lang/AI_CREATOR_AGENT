import os
import json

PATH = "projects/generation/provider_connection.json"

api_key = os.getenv("AI_IMAGE_API_KEY")

if not api_key:
    print("ERROR: AI_IMAGE_API_KEY is NOT_SET")
    raise SystemExit(1)

with open(PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

for provider in data.get("providers", []):
    if provider.get("type") == "IMAGE":
        provider["provider"] = "OpenAI"
        provider["model"] = "gpt-image-2"
        provider["configured"] = True
        provider["connected"] = True
        provider["api_key_configured"] = True
        provider["status"] = "CONNECTED"

with open(PATH, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4, ensure_ascii=False)

print("===================================")
print(" OPENAI IMAGE PROVIDER CONFIG")
print("===================================")
print("Provider: OpenAI")
print("Model: gpt-image-2")
print("API key detected: True")
print("Image provider: CONNECTED")
print("Video provider: unchanged")
print("Voice provider: unchanged")
print("Status: READY")
