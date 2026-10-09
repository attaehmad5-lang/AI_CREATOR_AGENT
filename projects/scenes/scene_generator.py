import json
from datetime import datetime

script_file = "projects/story/documentary_script.json"
output_file = "projects/scenes/documentary_scenes.json"

with open(script_file, "r", encoding="utf-8") as file:
    script = json.load(file)

scenes = []

for section in script["sections"]:
    scene = {
        "scene": section["section"],
        "title": section["title"],
        "narration": section["narration"],
        "visual_direction": "",
        "camera": "",
        "location": "",
        "lighting": "",
        "sound": "",
        "duration": "",
        "status": "READY"
    }

    scenes.append(scene)

output = {
    "project": script["project"],
    "status": "READY",
    "scene_count": len(scenes),
    "created_at": datetime.now().isoformat(),
    "scenes": scenes
}

with open(output_file, "w", encoding="utf-8") as file:
    json.dump(output, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("       SCENE GENERATOR")
print("===================================")
print()
print("Project:", output["project"])
print("Scene status:", output["status"])
print("Scenes created:", output["scene_count"])
print()
print("Scene file created successfully.")
print("Saved to:", output_file)
