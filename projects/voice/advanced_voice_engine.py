import json
from datetime import datetime

SCRIPT_FILE = "projects/story/documentary_script.json"
SCENE_FILE = "projects/scenes/documentary_scenes.json"
OUTPUT_FILE = "projects/voice/voice_prompts.json"

with open(SCRIPT_FILE, "r", encoding="utf-8") as f:
    script = json.load(f)

with open(SCENE_FILE, "r", encoding="utf-8") as f:
    scenes = json.load(f)

voice_items = []

for scene in scenes["scenes"]:
    section = script["sections"][scene["scene"] - 1]
    narration = section["narration"]

    voice_items.append({
        "scene": scene["scene"],
        "title": scene["title"],
        "narration": narration,
        "voice_style": "Deep professional cinematic documentary narrator",
        "delivery": "Natural, clear, controlled, emotionally intelligent delivery",
        "emotion": "Suspenseful, thoughtful, authentic, restrained emotion",
        "pacing": "Moderate pacing with deliberate pauses and strong emphasis on important words",
        "pause_direction": "Short pauses between key ideas; longer dramatic pause before major reveals",
        "music_direction": "Subtle cinematic ambient score with soft piano, atmospheric pads and restrained strings",
        "sound_direction": scene["sound"],
        "status": "READY"
    })

output = {
    "project": script["project"],
    "status": "READY",
    "engine": "Advanced Voice & Narration Engine",
    "language": "English",
    "voice_items": voice_items,
    "scene_count": len(voice_items),
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print(" ADVANCED VOICE & NARRATION ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Scenes processed:", output["scene_count"])
print("Narration tracks:", len(voice_items))
print("Status: READY")
print()
print("Voice prompt file created successfully.")
print("Saved to:", OUTPUT_FILE)
