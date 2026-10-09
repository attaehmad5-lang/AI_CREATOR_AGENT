import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

scenes = {
    "project": project_name,
    "status": "READY",
    "total_scenes": 10,
    "scenes": [
        {
            "scene": 1,
            "title": "Opening Hook",
            "purpose": "Capture viewer attention immediately",
            "duration": "00:00-02:00"
        },
        {
            "scene": 2,
            "title": "Introduction",
            "purpose": "Introduce the documentary subject",
            "duration": "02:00-04:00"
        },
        {
            "scene": 3,
            "title": "The Main Story",
            "purpose": "Present the central story",
            "duration": "04:00-08:00"
        },
        {
            "scene": 4,
            "title": "Behind The Scenes",
            "purpose": "Show how the documentary is filmed",
            "duration": "08:00-11:00"
        },
        {
            "scene": 5,
            "title": "Production Challenges",
            "purpose": "Show real filming difficulties",
            "duration": "11:00-14:00"
        },
        {
            "scene": 6,
            "title": "Cinematic Filming",
            "purpose": "Show advanced cinematic techniques",
            "duration": "14:00-17:00"
        },
        {
            "scene": 7,
            "title": "Human Story",
            "purpose": "Add emotional depth",
            "duration": "17:00-19:00"
        },
        {
            "scene": 8,
            "title": "The Final Moment",
            "purpose": "Build toward the conclusion",
            "duration": "19:00-21:00"
        },
        {
            "scene": 9,
            "title": "Conclusion",
            "purpose": "Deliver the final message",
            "duration": "21:00-23:00"
        },
        {
            "scene": 10,
            "title": "End Credits",
            "purpose": "Close the documentary professionally",
            "duration": "23:00-24:00"
        }
    ],
    "created_at": datetime.now().isoformat()
}

with open("projects/scenes/scenes.json", "w", encoding="utf-8") as file:
    json.dump(scenes, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("        SCENES DATA")
print("===================================")
print()
print("Project:", project_name)
print("Scenes file created successfully.")
print()
print("Total scenes:", scenes["total_scenes"])
print("Documentary structure:", "READY")
print()
print("Saved to: projects/scenes/scenes.json")
