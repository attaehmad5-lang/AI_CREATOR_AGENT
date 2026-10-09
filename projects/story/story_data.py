import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

story = {
    "project": project_name,
    "status": "READY",
    "title": "World Most Popular Documentary Filming",
    "genre": "Cinematic Documentary",
    "structure": [
        "Opening Hook",
        "Introduction",
        "Main Story",
        "Behind The Scenes",
        "Production Challenges",
        "Cinematic Filming",
        "Conclusion"
    ],
    "tone": [
        "Cinematic",
        "Emotional",
        "Informative",
        "Suspenseful"
    ],
    "target_duration": "20-30 minutes",
    "created_at": datetime.now().isoformat()
}

with open("projects/story/story.json", "w", encoding="utf-8") as file:
    json.dump(story, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("         STORY DATA")
print("===================================")
print()
print("Project:", project_name)
print("Story file created successfully.")
print()
print("Structure sections:", len(story["structure"]))
print("Tone elements:", len(story["tone"]))
print("Target duration:", story["target_duration"])
print()
print("Saved to: projects/story/story.json")
