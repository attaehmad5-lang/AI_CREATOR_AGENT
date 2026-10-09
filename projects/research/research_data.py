import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

research = {
    "project": project_name,
    "status": "READY",
    "topic": project_name,
    "audience": [
        "Documentary viewers",
        "Filmmaking fans",
        "Cinematic content viewers"
    ],
    "content_angles": [
        "How major documentaries are filmed",
        "Behind the scenes of documentary production",
        "Cinematic camera techniques",
        "Real locations and production challenges"
    ],
    "keywords": [
        "documentary filming",
        "documentary production",
        "behind the scenes",
        "cinematic documentary",
        "filmmaking"
    ],
    "created_at": datetime.now().isoformat()
}

with open("projects/research/research.json", "w", encoding="utf-8") as file:
    json.dump(research, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("       RESEARCH DATA")
print("===================================")
print()
print("Project:", project_name)
print("Research file created successfully.")
print()
print("Audience:", len(research["audience"]))
print("Content angles:", len(research["content_angles"]))
print("Keywords:", len(research["keywords"]))
print()
print("Saved to: projects/research/research.json")
