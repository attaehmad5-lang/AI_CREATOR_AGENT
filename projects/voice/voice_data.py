import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

voice = {
    "project": project_name,
    "status": "READY",
    "voice_style": "Cinematic Documentary Narration",
    "language": "English",
    "tone": [
        "Deep",
        "Professional",
        "Emotional",
        "Suspenseful",
        "Natural"
    ],
    "delivery": [
        "Clear narration",
        "Controlled pacing",
        "Emotional emphasis",
        "Dramatic pauses",
        "Strong storytelling"
    ],
    "music_direction": [
        "Cinematic ambient music",
        "Soft piano",
        "Deep strings",
        "Atmospheric pads"
    ],
    "created_at": datetime.now().isoformat()
}

with open("projects/voice/voice.json", "w", encoding="utf-8") as file:
    json.dump(voice, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("         VOICE DATA")
print("===================================")
print()
print("Project:", project_name)
print("Voice file created successfully.")
print()
print("Voice style:", voice["voice_style"])
print("Language:", voice["language"])
print("Tone elements:", len(voice["tone"]))
print("Delivery elements:", len(voice["delivery"]))
print("Music directions:", len(voice["music_direction"]))
print()
print("Saved to: projects/voice/voice.json")
