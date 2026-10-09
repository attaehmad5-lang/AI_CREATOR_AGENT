import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

visuals = {
    "project": project_name,
    "status": "READY",
    "visual_style": "Cinematic Documentary",
    "aspect_ratio": "16:9",
    "resolution": "4K",
    "camera_style": [
        "Wide establishing shots",
        "Medium cinematic shots",
        "Close-up emotional shots",
        "Drone aerial shots",
        "Slow cinematic camera movement"
    ],
    "lighting": [
        "Natural documentary lighting",
        "Golden hour",
        "Low-key dramatic lighting",
        "Soft cinematic highlights"
    ],
    "visual_elements": [
        "Real locations",
        "Documentary subjects",
        "Film crew",
        "Professional cinema cameras",
        "Natural environments",
        "Behind-the-scenes production"
    ],
    "created_at": datetime.now().isoformat()
}

with open("projects/visuals/visuals.json", "w", encoding="utf-8") as file:
    json.dump(visuals, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("        VISUALS DATA")
print("===================================")
print()
print("Project:", project_name)
print("Visuals file created successfully.")
print()
print("Visual style:", visuals["visual_style"])
print("Aspect ratio:", visuals["aspect_ratio"])
print("Resolution:", visuals["resolution"])
print("Camera styles:", len(visuals["camera_style"]))
print("Lighting styles:", len(visuals["lighting"]))
print()
print("Saved to: projects/visuals/visuals.json")
